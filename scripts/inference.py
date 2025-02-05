import argparse
import math
import pickle
import pprint
import time
from pathlib import Path

import joblib as jl
import torch
from scipy.signal import savgol_filter

from . import utils
from .data_loader.data_preprocessor import DataPreprocessor
from .pymo.preprocessing import *
from .pymo.viz_tools import *
from .pymo.writers import *
from .utils.data_utils import SubtitleWrapper, normalize_string
from .utils.train_utils import set_logger

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


def generate_gestures(args, pose_decoder, lang_model, words, seed_seq=None, verbose=1):
    out_list = []
    clip_length = words[-1][2]

    # pre seq
    pre_seq = torch.zeros((1, args.n_pre_poses, pose_decoder.pose_dim))
    if seed_seq is not None:
        pre_seq[0, :, :] = torch.Tensor(seed_seq[0:args.n_pre_poses])
    else:
        mean_pose = args.data_mean
        mean_pose = torch.squeeze(torch.Tensor(mean_pose))
        pre_seq[0, :, :] = mean_pose.repeat(args.n_pre_poses, 1)

    # divide into inference units and do inferences
    unit_time = args.n_poses / args.motion_resampling_framerate
    stride_time = (args.n_poses - args.n_pre_poses) / args.motion_resampling_framerate
    if clip_length < unit_time:
        num_subdivision = 1
    else:
        num_subdivision = math.ceil((clip_length - unit_time) / stride_time) + 1

    if verbose:
        print('{}, {}, {}, {}'.format(num_subdivision, unit_time, clip_length, stride_time))
    num_subdivision = min(num_subdivision, 59)  # DEBUG: generate only for the first N divisions

    out_poses = None
    start = time.time()
    for i in range(0, num_subdivision):
        start_time = i * stride_time
        end_time = start_time + unit_time

        # prepare text input
        word_seq = DataPreprocessor.get_words_in_time_range(word_list=words, start_time=start_time, end_time=end_time)
        word_indices = np.zeros(len(word_seq) + 2)
        word_indices[0] = lang_model.SOS_token
        word_indices[-1] = lang_model.EOS_token
        for w_i, word in enumerate(word_seq):
            if verbose:
                print(word[0], end=', ')
            word_indices[w_i + 1] = lang_model.get_word_index(word[0])
        if verbose:
            print(' ({}, {})'.format(start_time, end_time))
        in_text = torch.LongTensor(word_indices).unsqueeze(0).to(device)

        # prepare pre seq
        if i > 0:
            pre_seq[0, :, :] = out_poses.squeeze(0)[-args.n_pre_poses:]
        pre_seq = pre_seq.float().to(device)

        # inference
        words_lengths = torch.LongTensor([in_text.shape[1]]).to(device)
        out_poses = pose_decoder(in_text, words_lengths, pre_seq, None)
        out_seq = out_poses[0, :, :].data.cpu().numpy()

        # smoothing motion transition
        if len(out_list) > 0:
            last_poses = out_list[-1][-args.n_pre_poses:]
            out_list[-1] = out_list[-1][:-args.n_pre_poses]  # delete the last part

            for j in range(len(last_poses)):
                n = len(last_poses)
                prev = last_poses[j]
                next = out_seq[j]
                out_seq[j] = prev * (n - j) / (n + 1) + next * (j + 1) / (n + 1)

        out_list.append(out_seq)

    if verbose:
        print('Avg. inference time: {:.2} s'.format((time.time() - start) / num_subdivision))

    # aggregate results
    out_poses = np.vstack(out_list)

    return out_poses


def make_mocap_data(poses):
    pipeline = jl.load(os.path.join(os.path.dirname(__file__), '../resource/data_pipe.sav'))

    # smoothing
    n_poses = poses.shape[0]
    out_poses = np.zeros((n_poses, poses.shape[1]))

    for i in range(poses.shape[1]):
        out_poses[:, i] = savgol_filter(poses[:, i], 15, 2)  # NOTE: smoothing on rotation matrices is not optimal

    # rotation matrix to euler angles
    out_poses = out_poses.reshape((out_poses.shape[0], -1, 9))
    out_poses = out_poses.reshape((out_poses.shape[0], out_poses.shape[1], 3, 3))
    out_euler = np.zeros((out_poses.shape[0], out_poses.shape[1] * 3))
    for i in range(out_poses.shape[0]):  # frames
        r = R.from_matrix(out_poses[i])
        out_euler[i] = r.as_euler('ZXY', degrees=True).flatten()

    return pipeline.inverse_transform([out_euler])[0]


def make_bvh(mocap_data, save_path, filename_prefix):
    writer = BVHWriter()
    out_bvh_path = os.path.join(save_path, filename_prefix + '_generated.bvh')
    with open(out_bvh_path, 'w') as f:
        writer.write(mocap_data, f)


def main(checkpoint_path, transcript_path, vocab_cache_path):
    args, generator, loss_fn, lang_model, out_dim = utils.train_utils.load_checkpoint_and_model(checkpoint_path, device)
    pprint.pprint(vars(args))
    save_path = '../output/infer_sample'
    os.makedirs(save_path, exist_ok=True)

    # load lang_model
    with open(vocab_cache_path, 'rb') as f:
        lang_model = pickle.load(f)

    # prepare input
    transcript = SubtitleWrapper(transcript_path).get()

    word_list = []
    for wi in range(len(transcript)):
        word_s = float(transcript[wi]['start_time'][:-1])
        word_e = float(transcript[wi]['end_time'][:-1])
        word = transcript[wi]['word']

        word = normalize_string(word)
        if len(word) > 0:
            word_list.append([word, word_s, word_e])

    # inference
    out_poses = generate_gestures(args, generator, lang_model, word_list)

    # unnormalize
    mean = np.array(args.data_mean).squeeze()
    std = np.array(args.data_std).squeeze()
    std = np.clip(std, a_min=0.01, a_max=None)
    out_poses = np.multiply(out_poses, std) + mean

    # make mocap data:
    mocap_data = make_mocap_data(out_poses)

    # make a BVH
    filename_prefix = '{}'.format(transcript_path.stem)
    make_bvh(mocap_data, save_path, filename_prefix)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("ckpt_path", type=Path)
    parser.add_argument("transcript_path", type=Path)
    parser.add_argument("vocab_cache_path", type=Path)
    args = parser.parse_args()

    main(args.ckpt_path, args.transcript_path, args.vocab_cache_path)
