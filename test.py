"""General-purpose training script for image-to-image translation.

This script works for various models (with option '--model': e.g., pix2pix, cyclegan, colorization) and
different datasets (with option '--dataset_mode': e.g., aligned, unaligned, single, colorization).
You need to specify the dataset ('--dataroot'), experiment name ('--name'), and model ('--model').

It first creates model, dataset, and visualizer given the option.
It then does standard network training. During the training, it also visualize/save the images, print/save the loss plot, and save models.
The script supports continue/resume training. Use '--continue_train' to resume your previous training.

Example:
    Train a CycleGAN model:
        python train.py --dataroot ./datasets/maps --name maps_cyclegan --model cycle_gan
    Train a pix2pix model:
        python train.py --dataroot ./datasets/facades --name facades_pix2pix --model pix2pix --direction BtoA

See options/base_options.py and options/train_options.py for more training options.
See training and test tips at: https://github.com/junyanz/pytorch-CycleGAN-and-pix2pix/blob/master/docs/tips.md
See frequently asked questions at: https://github.com/junyanz/pytorch-CycleGAN-and-pix2pix/blob/master/docs/qa.md
"""
import time
# from data import create_dataset
from models import create_model
from util.visualizer import Visualizer
from dataset.datahandler import Loader
import yaml
import argparse
import numpy as np
import torch
from tqdm import trange
import tqdm
from collections import defaultdict
import matplotlib.pyplot as plt
import os

def cycle(iterable):
    while True:
        for x in iterable:
            yield x


class M_parser():
    def __init__(self, cfg_path, data_dir):
        opt_dict = yaml.safe_load(open(cfg_path, 'r'))
        for k , v in opt_dict.items():
            setattr(self, k, v)
        if data_dir != '':
            self.dataset['dataset_A']['data_dir'] = data_dir
        self.isTrain = False



if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--cfg_test', type=str, help='Path of the config file')
    parser.add_argument('--data_dir', type=str, default='', help='Path of the dataset')
    pa = parser.parse_args()
    opt = M_parser(pa.cfg_test, pa.data_dir)
    torch.manual_seed(opt.seed)
    np.random.seed(opt.seed)
    # DATA = yaml.safe_load(open(pa.cfg_dataset, 'r'))
    ## test whole code fast

    model = create_model(opt)      # create a model given opt.model and other options
    model.setup(opt)               # regular setup: load and print networks; create schedulers
    visualizer = Visualizer(opt)   # create a visualizer that display/save images and plots
    g_steps = 0
    KL = Loader(data_dict=opt.dataset, batch_size=opt.batch_size,\
         val_split_ratio=opt.val_split_ratio, max_dataset_size=opt.max_dataset_size, workers= opt.n_workers, is_train=False)

    e_steps = 0                  # the number of training iterations in current epoch, reset to 0 every epoch
    visualizer.reset()              # reset the visualizer: make sure it saves the results to HTML at least once every epoch
    test_dl = iter(KL.testloader)
    n_test_batch = len(KL.testloader)

    test_losses = defaultdict(list)
    test_image_results = defaultdict(list)
    model.train(False)
    tq = tqdm.tqdm(total=n_test_batch, desc='val_Iter', position=5)
    n_pics = 0
    for i in range(n_test_batch):
        data = next(test_dl)
        model.set_input_PCL(data)
        with torch.no_grad():
            model.evaluate_model()
        for k ,v in model.get_current_losses(is_eval=True).items():
            test_losses[k].append(v)
        for k, v in model.get_current_visuals().items():
            test_image_results[k].append(v.cpu().detach().numpy())
            n_pics += v.shape[0]
        tq.update(1)
    test_image_results = {k: np.concatenate(v, axis=0) for k, v in test_image_results.items()}
    losses = {k: np.array(v).mean() for k , v in test_losses.items()}
    print (losses)
    exp_name = os.path.join(opt.checkpoints_dir, opt.name, 'test_results_pics')
    os.makedirs(exp_name, exist_ok=True)
    n_pics = min(n_pics , 100)
    n_keys = len(test_image_results.keys())
    n_pics = n_pics // n_keys
    ra = test_image_results['real_A']
    n_keys = n_keys if ra.shape[1] > 3 else n_keys + 2
    for i in range(n_pics):
        fig = plt.figure()
        ind = 0
        for k, img in test_image_results.items():
            if k == 'real_A' and img.shape[1] > 3:
                rgb = img[:, 3:]
                ax = fig.add_subplot(1, n_keys, ind+1)
                ax.imshow((rgb[i]*0.5 + 0.5).transpose((1, 2, 0)))
                ax.title.set_text('rgb')
                ax.set_xticks([])
                ax.set_yticks([])
                img = img[:, :3]
                ind += 1
                continue

            for j in range(img.shape[1]):
                ax = fig.add_subplot(1, n_keys, ind+1)
                ax.imshow((img[i][j]*0.5 + 0.5),
                            cmap='inferno' if k == 'range' else 'cividis', vmin=0.0, vmax=1.0)
                ax.title.set_text(k)
                ax.set_xticks([])
                ax.set_yticks([])
                ind+= 1
        fname = os.path.join(exp_name, 'img_' + str(i) + '.png' )                    
        plt.savefig(fname)
        plt.close(fig)