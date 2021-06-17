from random import seed
import torch
import torch.nn as nn
import torch.optim as optim
from util import Logger, AverageMeter, save_checkpoint, save_tensor_img, set_seed
import os
import numpy as np
from matplotlib import pyplot as plt
import time
import argparse
from tqdm import tqdm
from dataset import get_loader
import torchvision.utils as vutils

import torch.nn.functional as F
import pytorch_toolbelt.losses as PTL

from models.GCoNet import GCoNet
from config import Config
from loss import saliency_structure_consistency
from util import generate_smoothed_gt

from evaluation.dataloader import EvalDataset
from evaluation.evaluator import Eval_thread

from PIL import ImageFile
ImageFile.LOAD_TRUNCATED_IMAGES = True

# Parameter from command line
parser = argparse.ArgumentParser(description='')
parser.add_argument('--model',
                    default='GCoNet',
                    type=str,
                    help="Options: '', ''")
parser.add_argument('--loss',
                    default='DSLoss_IoU_noCAM',
                    type=str,
                    help="Options: '', ''")
parser.add_argument('--bs', '--batch_size', default=48, type=int)
parser.add_argument('--lr',
                    '--learning_rate',
                    default=3e-4,
                    type=float,
                    help='Initial learning rate')
parser.add_argument('--resume',
                    default=None,
                    type=str,
                    help='path to latest checkpoint')
parser.add_argument('--epochs', default=50, type=int)
parser.add_argument('--start_epoch',
                    default=0,
                    type=int,
                    help='manual epoch number (useful on restarts)')
parser.add_argument('--trainset',
                    default='Jigsaw2_DUTS',
                    type=str,
                    help="Options: 'Jigsaw2_DUTS', 'DUTS_class'")
parser.add_argument('--size',
                    default=224,
                    type=int,
                    help='input size')
parser.add_argument('--ckpt_dir', default=None, help='Temporary folder')

parser.add_argument('--testsets',
                    default='CoCA+CoSOD3k+CoSal2015',
                    type=str,
                    help="Options: 'CoCA','CoSal2015','CoSOD3k','iCoseg','MSRC'")

args = parser.parse_args()


# Prepare dataset
if args.trainset == 'Jigsaw2_DUTS':
    train_img_path = '../Dataset/Jigsaw2_DUTS/img/'
    train_gt_path = '../Dataset/Jigsaw2_DUTS/gt/'
    train_loader = get_loader(train_img_path,
                              train_gt_path,
                              args.size,
                              1,
                              max_num=args.bs,
                              istrain=True,
                              shuffle=False,
                              num_workers=4,
                              pin=True)
elif args.trainset == 'DUTS_class':
    root_dir = '../../../datasets/sod'
    train_img_path = os.path.join(root_dir, 'images/DUTS_class')
    train_gt_path = os.path.join(root_dir, 'gts/DUTS_class')
    train_loader = get_loader(train_img_path,
                              train_gt_path,
                              args.size,
                              1,
                              max_num=args.bs,
                              istrain=True,
                              shuffle=False,
                              num_workers=8,
                              pin=True)
else:
    print('Unkonwn train dataset')
    print(args.dataset)

test_loaders = {}
for testset in args.testsets.split('+'):
    test_loader = get_loader(
        os.path.join('../../../datasets/sod', 'images', testset), os.path.join('../../../datasets/sod', 'gts', testset),
        args.size, 1, istrain=False, shuffle=False, num_workers=8, pin=True
    )
    test_loaders[testset] = test_loader


if Config().rand_seed:
    set_seed(Config().rand_seed)

# make dir for ckpt
os.makedirs(args.ckpt_dir, exist_ok=True)

# Init log file
logger = Logger(os.path.join(args.ckpt_dir, "log.txt"))

# Init model
device = torch.device("cuda")

model = GCoNet()
model = model.to(device)

backbone_params = list(map(id, model.bb.parameters()))
base_params = filter(lambda p: id(p) not in backbone_params,
                     model.parameters())

all_params = [{'params': base_params}, {'params': model.bb.parameters(), 'lr': args.lr * 0.01}]

# Setting optimizer
optimizer = optim.Adam(params=all_params, lr=args.lr, betas=[0.9, 0.99])
scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=Config().decay_step_size, gamma=0.1)

# Why freeze the backbone?...
if Config().freeze:
    for key, value in model.named_parameters():
        if 'bb' in key and 'bb.conv5.conv5_3' not in key:
            value.requires_grad = False


# log model and optimizer pars
logger.info("Model details:")
logger.info(model)
logger.info("Optimizer details:")
logger.info(optimizer)
logger.info("Scheduler details:")
logger.info(scheduler)
logger.info("Other hyperparameters:")
logger.info(args)

# Setting Loss
exec('from loss import ' + args.loss)
dsloss = eval(args.loss+'()')


def main():
    val_e_measure = []

    # Optionally resume from a checkpoint
    if args.resume:
        if os.path.isfile(args.resume):
            logger.info("=> loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume)
            args.start_epoch = checkpoint['epoch']
            model.load_state_dict(checkpoint['state_dict'])
            optimizer.load_state_dict(checkpoint['optimizer'])
            scheduler.load_state_dict(checkpoint['scheduler'])
            logger.info("=> loaded checkpoint '{}' (epoch {})".format(
                args.resume, checkpoint['epoch']))
        else:
            logger.info("=> no checkpoint found at '{}'".format(args.resume))

    for epoch in range(args.start_epoch, args.epochs):
        train_loss = train(epoch)
        e_measures = validate(model, test_loaders, args.testsets)
        val_e_measure.append(e_measures)
        print('Validation: E_max on CoCA for epoch-{} is {:.4f}. Best epoch is epoch-{} with E_max {:.4f}'.format(
            epoch, e_measures[0], np.argmin(np.array(val_e_measure)[:, 0].squeeze()), np.min(np.array(val_e_measure)[:, 0]))
        )
        # Save checkpoint
        save_checkpoint(
            {
                'epoch': epoch + 1,
                'state_dict': model.state_dict(),
                'scheduler': scheduler.state_dict(),
            },
            path=args.ckpt_dir)
        if epoch >= args.epochs - Config().val_last:
            torch.save(model.state_dict(), os.path.join(args.ckpt_dir, 'ep{}.pth'.format(epoch)))
        if np.max(val_e_measure) == e_measures:
            best_weights_before = [os.path.join(args.ckpt_dir, weight_file) for weight_file in os.listdir(args.ckpt_dir) if 'best_' in weight_file]
            for best_weight_before in best_weights_before:
                os.remove(best_weight_before)
            torch.save(model.state_dict(), os.path.join(args.ckpt_dir, 'best_ep{}_emax{:.4f}.pth'.format(epoch, e_measures)))

def train(epoch):
    loss_log = AverageMeter()

    # Switch to train mode
    model.train()
    #CE = torch.nn.BCEWithLogitsLoss()
    FL = PTL.BinaryFocalLoss()

    for batch_idx, batch in enumerate(train_loader):
        inputs = batch[0].to(device).squeeze(0)
        gts = batch[1].to(device).squeeze(0)
        cls_gts = torch.LongTensor(batch[-1]).to(device)
        
        gts_neg = torch.full_like(gts, 0.0)
        gts_cat = torch.cat([gts, gts_neg], dim=0)
        scaled_preds, pred_cls, pred_x5 = model(inputs)
        atts = scaled_preds[-1]

        if Config().label_smoothing:
            loss_sal = 0.5 * dsloss(scaled_preds, gts) + dsloss(scaled_preds, generate_smoothed_gt(gts))
        else:
            loss_sal = dsloss(scaled_preds, gts)
        if Config().self_supervision:
            H, W = inputs.shape[-2:]
            images_scale = F.interpolate(inputs, size=(H//4, W//4), mode='bilinear', align_corners=True)
            sal_scale = model(images_scale)[0][-1]
            sal_s = F.interpolate(atts, size=(H//4, W//4), mode='bilinear', align_corners=True)
            loss_ss = saliency_structure_consistency(sal_scale, sal_s)
            loss_sal += loss_ss * 0.3

        loss_cls = F.cross_entropy(pred_cls, cls_gts) * 3.0
        loss_x5 = FL(pred_x5, gts_cat) * 250.0
        loss = loss_sal + loss_cls + loss_x5

        loss_log.update(loss, inputs.size(0))

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        if batch_idx % 20 == 0:
            # NOTE: Top2Down; [0] is the grobal slamap and [5] is the final output
            logger.info('Epoch[{0}/{1}] Iter[{2}/{3}]  '
                        'Train Loss: loss_sal: {4:.3f}, loss_cls: {5:.3f}, loss_x5: {6:.3f} '
                        'Loss_total: {loss.val:.3f} ({loss.avg:.3f})  '.format(
                            epoch,
                            args.epochs,
                            batch_idx,
                            len(train_loader),
                            loss_sal,
                            loss_cls,
                            loss_x5,
                            loss=loss_log,
                        ))
    scheduler.step()
    logger.info('@==Final== Epoch[{0}/{1}]  '
                'Train Loss: {loss.avg:.3f}  '.format(epoch,
                                                      args.epochs,
                                                      loss=loss_log))

    return loss_log.avg


def validate(model, test_loaders, testsets):
    model.eval()

    testsets = testsets.split('+')
    e_measures = []
    for testset in testsets[:1]:
        print('Validating {}...'.format(testset))
        test_loader = test_loaders[testset]
        
        saved_root = os.path.join('tmp4val', testset)

        for batch in test_loader:
            inputs = batch[0].to(device).squeeze(0)
            gts = batch[1].to(device).squeeze(0)
            subpaths = batch[2]
            ori_sizes = batch[3]
            with torch.no_grad():
                scaled_preds = model(inputs)[-1]

            os.makedirs(os.path.join(saved_root, subpaths[0][0].split('/')[0]), exist_ok=True)

            num = len(scaled_preds)
            for inum in range(num):
                subpath = subpaths[inum][0]
                ori_size = (ori_sizes[inum][0].item(), ori_sizes[inum][1].item())
                res = nn.functional.interpolate(scaled_preds[inum].unsqueeze(0), size=ori_size, mode='bilinear', align_corners=True)
                save_tensor_img(res, os.path.join(saved_root, subpath))

        eval_loader = EvalDataset(
            saved_root,                                                             # preds
            os.path.join('/home/pz1/datasets/sod/gts', testset)                     # GT
        )
        evaler = Eval_thread(eval_loader, cuda=True)
        # Use E_measure for validation
        e_measure = evaler.Eval_Emeasure().max().item()
        e_measures.append(e_measure)

    model.train()
    return e_measures

if __name__ == '__main__':
    main()
