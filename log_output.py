import time
# from data import create_dataset
from models import create_model
from util.visualizer import Visualizer
from fid import FID
from dataset.datahandler import get_data_loader
from rangenet.tasks.semantic.modules.segmentator import *
import yaml
import argparse
import numpy as np
import torch
import tqdm
import os
from util.lidar import LiDAR
from util import *
from collections import defaultdict
import shutil
from util.sampling.fps import downsample_point_clouds
from util.metrics.cov_mmd_1nna import compute_cov_mmd_1nna
from util.metrics.jsd import compute_jsd
from util.metrics.swd import compute_swd
from util.metrics.seg_accuracy import compute_seg_accuracy

import random


os.environ['LD_PRELOAD'] = "/usr/lib/x86_64-linux-gnu/libstdc++.so.6" 
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'

def cycle(iterable):
    while True:
        for x in iterable:
            yield x

def inv_to_xyz(inv, lidar, tol=1e-8):
    inv = tanh_to_sigmoid(inv).clamp_(0, 1)
    xyz = lidar.inv_to_xyz(inv, tol)
    xyz = xyz.flatten(2).transpose(1, 2)  # (B,N,3)
    xyz = downsample_point_clouds(xyz, 512)
    return xyz



class M_parser():
    def __init__(self, cfg_path, data_dir, data_dir_B, load):
        opt_dict = yaml.safe_load(open(cfg_path, 'r'))
        dict_class = make_class_from_dict(opt_dict)
        members = [attr for attr in dir(dict_class) if not callable(getattr(dict_class, attr)) and not attr.startswith("__")]
        for m in members:
            setattr(self, m, getattr(dict_class, m))
        if data_dir != '':
            self.dataset.dataset_A.data_dir = data_dir
        if data_dir_B != '':
            self.dataset.dataset_B.data_dir = data_dir_B
        self.model.isTrain = self.training.isTrain = not self.training.test
        self.training.epoch_decay = self.training.n_epochs//2



def modify_opt_for_fast_test(opt):
    opt.n_epochs = 2
    opt.epoch_decay = opt.n_epochs//2
    opt.display_freq = 1
    opt.print_freq = 1
    opt.save_latest_freq = 1
    opt.max_dataset_size = 10
    opt.batch_size = 2


def check_exp_exists(opt, cfg_args):
    cfg_path = cfg_args.cfg
    opt_t = opt.training
    opt_m = opt.model
    opt_d = opt.dataset.dataset_A
    modality_A = '_'.join(opt_m.modality_A)
    if hasattr(opt_m, 'modality_cond'):
        cond_modality = '_'.join(opt_m.modality_cond)
    out_ch = '_'.join(opt_m.out_ch)
    if cfg_args.load != '':
        # opt_t.name = cfg_path.split(os.sep)[1]
        opt_t.name = cfg_args.load
    elif cfg_args.fast_test:
        opt_t.name = 'test'
    else:
        if 'pix2pix' in opt_m.name:
            opt_t.name = f'pix2pix_modality_A_{modality_A}_out_ch_{out_ch}_L_L1_{opt_m.lambda_L1}' \
                + f'_L_GAN_{opt_m.lambda_LGAN}_L_mask_{opt_m.lambda_mask}_w_{opt_d.img_prop.width}_h_{opt_d.img_prop.height}' \
                    + f'_netG_{opt_m.netG}_netD_{opt_m.netD}_batch_size_{opt_t.batch_size}_finesize_{opt_d.img_prop.finesize}'
        elif 'cycle_gan' in opt_m.name:
            opt_t.name = f'cycle_gan_modality_A_{modality_A}_out_ch_{out_ch}_lambda_A_{opt_m.lambda_A}_lambda_B_{opt_m.lambda_B}_lambda_idt_{opt_m.lambda_idt}' \
                + f'_w_{opt_d.img_prop.width}_h_{opt_d.img_prop.height}' \
                    + f'_netG_{opt_m.netG}_netD_{opt_m.netD}_batch_size_{opt_t.batch_size}_finesize_{opt_d.img_prop.finesize}'
        elif 'gc_gan' in opt_m.name:
            opt_t.name = f'gc_gan_modality_A_{modality_A}_out_ch_{out_ch}_lambda_idt_{opt_m.identity}_lambda_AB_{opt_m.lambda_AB}' \
                + f'_lambda_gc_{opt_m.lambda_gc}_lambda_G_{opt_m.lambda_G}_w_{opt_d.img_prop.width}_h_{opt_d.img_prop.height}' \
                    + f'_netG_{opt_m.netG}_netD_{opt_m.netD}_batch_size_{opt_t.batch_size}_finesize_{opt_d.img_prop.finesize}'
        elif 'cut' in opt_m.name:
            opt_t.name = f'cut_modality_A_{modality_A}_out_ch_{out_ch}_cond_modality_{cond_modality}_lambda_GAN_{opt_m.lambda_GAN}' \
                + f'_lambda_NCE_{opt_m.lambda_NCE}_lambda_NCE_feat_{opt_m.lambda_NCE_feat}_w_{opt_d.img_prop.width}_h_{opt_d.img_prop.height}' \
                    + f'_netG_{opt_m.netG}_netD_{opt_m.netD}_netF_{opt_m.netF}_n_layers_D_{opt_m.n_layers_D}_batch_size_{opt_t.batch_size}_finesize_{opt_d.img_prop.finesize}_lr_decay_iters_{opt_t.lr_decay_iters}'
        
    exp_dir = os.path.join(opt_t.checkpoints_dir, opt_t.name)
    if not opt_t.continue_train and opt_t.isTrain:
        if os.path.exists(exp_dir):
            reply = ''
            # raise Exception('Checkpoint exists!!')
            while not reply.startswith('y') and not reply.startswith('n'):
                reply = str(input(f'exp_dir {exp_dir} exists. Do you want to delete it? (y/n): \n')).lower().strip()
            if reply.startswith('y'):
                shutil.rmtree(exp_dir)
            else:
                print('Please Re-run the program with \"continue train\" enabled')
                exit(0)
        os.makedirs(exp_dir, exist_ok=True)
        shutil.copy(cfg_path, exp_dir)
    else:
        assert os.path.exists(exp_dir)

def main(runner_cfg_path=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--cfg', type=str, default='', help='Path of the config file')
    parser.add_argument('--data_dir', type=str, default='', help='Path of the dataset A')
    parser.add_argument('--data_dir_B', type=str, default='', help='Path of the dataset B')
    parser.add_argument('--fast_test', action='store_true', help='fast test of experiment')
    parser.add_argument('--load', type=str, default='', help='the name of the experiment folder while loading the experiment')
    parser.add_argument('--on_input', action='store_true', help='unsupervised metrics is computerd on dataset A')
    parser.add_argument('--no_inv', action='store_true', help='use it to calc unsupervised metrics on input inv, in case modality_B does not contain inv')
    cl_args = parser.parse_args()
    if runner_cfg_path is not None:
        cl_args.cfg = runner_cfg_path
    
    split = 'train'
    seqs = [0, 0 , 0] if cl_args.fast_test else [0, 0 , 0]
    ids = [1, 2, 3] if cl_args.fast_test else [1, 2, 3]
    opt = M_parser(cl_args.cfg, cl_args.data_dir, cl_args.data_dir_B, cl_args.load)
    torch.manual_seed(opt.training.seed)
    np.random.seed(opt.training.seed)
    random.seed(opt.training.seed)
        
    # DATA = yaml.safe_load(open(pa.cfg_dataset, 'r'))
    ## test whole code fast
    if cl_args.fast_test and opt.training.isTrain:
        modify_opt_for_fast_test(opt.training)
    if not opt.training.isTrain:
        opt.training.n_epochs = 1
    check_exp_exists(opt, cl_args)
    is_two_dataset = False
    if hasattr(opt.dataset, 'dataset_B'):
        is_two_dataset = True
    device = torch.device('cuda:{}'.format(opt.training.gpu_ids[0])) if opt.training.gpu_ids else torch.device('cpu') 
    ds_cfg = make_class_from_dict(yaml.safe_load(open(f'configs/dataset_cfg/{opt.dataset.dataset_A.name}_cfg.yml', 'r')))
    if not hasattr(opt.dataset.dataset_A, 'data_dir'):
        opt.dataset.dataset_A.data_dir = ds_cfg.data_dir
    if is_two_dataset:
        if not hasattr(opt.dataset.dataset_B, 'data_dir'):
            ds_cfg_B = make_class_from_dict(yaml.safe_load(open(f'configs/dataset_cfg/{opt.dataset.dataset_B.name}_cfg.yml', 'r')))
            opt.dataset.dataset_B.data_dir = ds_cfg_B.data_dir
    
    lidar_A = LiDAR(
    cfg=ds_cfg,
    height=opt.dataset.dataset_A.img_prop.height,
    width=opt.dataset.dataset_A.img_prop.width).to(device)
    lidar_B = LiDAR(
    cfg=ds_cfg_B,
    height=opt.dataset.dataset_B.img_prop.height,
    width=opt.dataset.dataset_B.img_prop.width,
   ).to(device) if is_two_dataset else None
    visualizer = Visualizer(opt.training)   # create a visualizer that display/save images and plots
    g_steps = 0
    ignore_label = [0, 2, 3, 4, 5, 6, 7, 8, 10, 12, 16]


    val_dl, val_dataset = get_data_loader(opt, split, opt.training.batch_size, shuffle=False)
    dataset_A_datalist = np.array(val_dataset.datasetA.datalist)
    dataset_A_selected_idx = []
    for seq, id in zip(seqs, ids):
        pcl_file_path = os.path.join(ds_cfg.data_dir, 'sequences', str(seq).zfill(2), 'velodyne', str(id).zfill(6)+('.bin' if ds_cfg.is_raw else '.npy'))
        dataset_A_selected_idx.append(np.where(dataset_A_datalist == pcl_file_path)[0][0])
    
    # test_dl, test_dataset = get_data_loader(opt, 'test', opt.training.batch_size, dataset_name=cl_args.ref_dataset_name, two_dataset_enabled=False)
    with torch.no_grad():
        seg_model = Segmentator().to(device)
    model = create_model(opt, lidar_A, lidar_B)      # create a model given opt.model and other options
    model.set_seg_model(seg_model)               # regular setup: load and print networks; create schedulers
    ## initilisation of the model for netF in cut
    val_dl_iter = iter(val_dl); data = next(val_dl_iter); model.data_dependent_initialize(data)
    model.setup(opt.training)
    # n_test_batch = 2 if cl_args.fast_test else  len(test_dl)
    # test_dl_iter = iter(test_dl)
    data_dict = defaultdict(list)
    # N = 2 * opt.training.batch_size if cl_args.fast_test else min(len(test_dataset), len(val_dataset), 1000)
    start_from_epoch = model.schedulers[0].last_epoch if opt.training.continue_train else 0 
    val_dl_iter = iter(val_dl)
    n_val_batch = 2 if cl_args.fast_test else  len(val_dl)
    ##### validation
    val_losses = defaultdict(list)
    model.train(False)
    tag = 'val' if opt.training.isTrain else 'test'
    val_tq = tqdm.tqdm(total=len(dataset_A_selected_idx), desc='val_Iter', position=5)
    for i, idx in enumerate(dataset_A_selected_idx):
        data = val_dataset[idx]
        data['A'] = {k: v.unsqueeze(0) for k, v in data['A'].items()}
        data['B'] = {k: v.unsqueeze(0) for k, v in data['B'].items()}
        model.set_input(data)
        with torch.no_grad():
            model.forward()
        fetched_data = fetch_reals(data['A'] if is_two_dataset else data, lidar_A, device)
        if cl_args.on_input:
            # assert is_two_dataset == False
            if 'inv' in fetched_data:
                synth_inv = fetched_data['inv']
            if 'reflectance' in fetched_data:
                synth_reflectance = fetched_data['reflectance']
            if 'mask' in fetched_data:
                synth_mask = fetched_data['mask']
        else:
            if hasattr(model, 'synth_reflectance'):
                synth_reflectance = model.synth_reflectance 
            if hasattr(model, 'synth_mask'):
                synth_mask = model.synth_mask
            if hasattr(model, 'synth_inv') and not cl_args.no_inv:
                synth_inv = model.synth_inv
            else:
                synth_inv = fetched_data['inv'] * synth_mask
        synth_depth = lidar_A.revert_depth(tanh_to_sigmoid(synth_inv), norm=False)
        synth_points = lidar_A.inv_to_xyz(tanh_to_sigmoid(synth_inv)) * lidar_A.max_depth
        synth_reflectance = tanh_to_sigmoid(synth_reflectance)
        synth_data = torch.cat([synth_depth, synth_points, synth_reflectance, synth_mask], dim=1)
        pred, _ = seg_model(synth_data)
        pred = pred.argmax(dim=1)
        current_visuals = model.get_current_visuals()
        current_visuals['pred_label'] = pred
        current_visuals = {k: v for k ,v in current_visuals.items() if 'B' not in k}
        seq = seqs[i]
        id = ids[i]
        if is_two_dataset:
            visualizer.display_current_results('',current_visuals, (seq, id),ds_cfg, opt.dataset.dataset_A.name, lidar_A, ds_cfg_B,\
                    opt.dataset.dataset_B.name,lidar_B, save_img=True)
        else:
            visualizer.display_current_results('', current_visuals, (seq, id),ds_cfg, opt.dataset.dataset_A.name, lidar_A, save_img=True)
        val_tq.update(1)


if __name__ == '__main__':
    main()
    
 