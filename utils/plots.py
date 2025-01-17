# YOLOv5 🚀 by Ultralytics, GPL-3.0 license
"""
Plotting utils
"""

import contextlib
import math
import os
from copy import copy
from pathlib import Path
from urllib.error import URLError

import cv2
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sn
import torch
from PIL import Image, ImageDraw, ImageFont

from utils import TryExcept, threaded
from utils.general import (CONFIG_DIR, FONT, LOGGER, check_font, check_requirements, clip_boxes, increment_path,
                           is_ascii, xywh2xyxy, xyxy2xywh)
from utils.metrics import fitness
from utils.segment.general import scale_image
from sklearn import metrics as ms
from collections import Counter
import collections

# Settings
RANK = int(os.getenv('RANK', -1))
matplotlib.rc('font', **{'size': 11})
matplotlib.use('Agg')  # for writing to files only


class Colors:
    # Ultralytics color palette https://ultralytics.com/
    def __init__(self):
        # hex = matplotlib.colors.TABLEAU_COLORS.values()
        hexs = ('FF3838', 'FF9D97', 'FF701F', 'FFB21D', 'CFD231', '48F90A', '92CC17', '3DDB86', '1A9334', '00D4BB',
                '2C99A8', '00C2FF', '344593', '6473FF', '0018EC', '8438FF', '520085', 'CB38FF', 'FF95C8', 'FF37C7')
        self.palette = [self.hex2rgb(f'#{c}') for c in hexs]
        self.n = len(self.palette)

    def __call__(self, i, bgr=False):
        c = self.palette[int(i) % self.n]
        return (c[2], c[1], c[0]) if bgr else c

    @staticmethod
    def hex2rgb(h):  # rgb order (PIL)
        return tuple(int(h[1 + i:1 + i + 2], 16) for i in (0, 2, 4))


colors = Colors()  # create instance for 'from utils.plots import colors'


def check_pil_font(font=FONT, size=10):
    # Return a PIL TrueType Font, downloading to CONFIG_DIR if necessary
    font = Path(font)
    font = font if font.exists() else (CONFIG_DIR / font.name)
    try:
        return ImageFont.truetype(str(font) if font.exists() else font.name, size)
    except Exception:  # download if missing
        try:
            check_font(font)
            return ImageFont.truetype(str(font), size)
        except TypeError:
            check_requirements('Pillow>=8.4.0')  # known issue https://github.com/ultralytics/yolov5/issues/5374
        except URLError:  # not online
            return ImageFont.load_default()


class Annotator:
    # YOLOv5 Annotator for train/val mosaics and jpgs and detect/hub inference annotations
    def __init__(self, im, line_width=None, font_size=None, font='Arial.ttf', pil=False, example='abc'):
        assert im.data.contiguous, 'Image not contiguous. Apply np.ascontiguousarray(im) to Annotator() input images.'
        non_ascii = not is_ascii(example)  # non-latin labels, i.e. asian, arabic, cyrillic
        self.pil = pil or non_ascii
        if self.pil:  # use PIL
            self.im = im if isinstance(im, Image.Image) else Image.fromarray(im)
            self.draw = ImageDraw.Draw(self.im)
            self.font = check_pil_font(font='Arial.Unicode.ttf' if non_ascii else font,
                                       size=font_size or max(round(sum(self.im.size) / 2 * 0.035), 12))
        else:  # use cv2
            self.im = im
        self.lw = line_width or max(round(sum(im.shape) / 2 * 0.003), 2)  # line width

    def box_label(self, box, label='', color=(128, 128, 128), txt_color=(255, 255, 255)):
        # Add one xyxy box to image with label
        if self.pil or not is_ascii(label):
            self.draw.rectangle(box, width=self.lw, outline=color)  # box
            if label:
                w, h = self.font.getsize(label)  # text width, height
                outside = box[1] - h >= 0  # label fits outside box
                self.draw.rectangle(
                    (box[0], box[1] - h if outside else box[1], box[0] + w + 1,
                     box[1] + 1 if outside else box[1] + h + 1),
                    fill=color,
                )
                # self.draw.text((box[0], box[1]), label, fill=txt_color, font=self.font, anchor='ls')  # for PIL>8.0
                self.draw.text((box[0], box[1] - h if outside else box[1]), label, fill=txt_color, font=self.font)
        else:  # cv2
            p1, p2 = (int(box[0]), int(box[1])), (int(box[2]), int(box[3]))
            cv2.rectangle(self.im, p1, p2, color, thickness=self.lw, lineType=cv2.LINE_AA)
            if label:
                tf = max(self.lw - 1, 1)  # font thickness
                w, h = cv2.getTextSize(label, 0, fontScale=self.lw / 3, thickness=tf)[0]  # text width, height
                outside = p1[1] - h >= 3
                p2 = p1[0] + w, p1[1] - h - 3 if outside else p1[1] + h + 3
                cv2.rectangle(self.im, p1, p2, color, -1, cv2.LINE_AA)  # filled
                cv2.putText(self.im,
                            label, (p1[0], p1[1] - 2 if outside else p1[1] + h + 2),
                            0,
                            self.lw / 3,
                            txt_color,
                            thickness=tf,
                            lineType=cv2.LINE_AA)

    def masks(self, masks, colors, im_gpu=None, alpha=0.5):
        """Plot masks at once.
        Args:
            masks (tensor): predicted masks on cuda, shape: [n, h, w]
            colors (List[List[Int]]): colors for predicted masks, [[r, g, b] * n]
            im_gpu (tensor): img is in cuda, shape: [3, h, w], range: [0, 1]
            alpha (float): mask transparency: 0.0 fully transparent, 1.0 opaque
        """
        if self.pil:
            # convert to numpy first
            self.im = np.asarray(self.im).copy()
        if im_gpu is None:
            # Add multiple masks of shape(h,w,n) with colors list([r,g,b], [r,g,b], ...)
            if len(masks) == 0:
                return
            if isinstance(masks, torch.Tensor):
                masks = torch.as_tensor(masks, dtype=torch.uint8)
                masks = masks.permute(1, 2, 0).contiguous()
                masks = masks.cpu().numpy()
            # masks = np.ascontiguousarray(masks.transpose(1, 2, 0))
            masks = scale_image(masks.shape[:2], masks, self.im.shape)
            masks = np.asarray(masks, dtype=np.float32)
            colors = np.asarray(colors, dtype=np.float32)  # shape(n,3)
            s = masks.sum(2, keepdims=True).clip(0, 1)  # add all masks together
            masks = (masks @ colors).clip(0, 255)  # (h,w,n) @ (n,3) = (h,w,3)
            self.im[:] = masks * alpha + self.im * (1 - s * alpha)
        else:
            if len(masks) == 0:
                self.im[:] = im_gpu.permute(1, 2, 0).contiguous().cpu().numpy() * 255
            colors = torch.tensor(colors, device=im_gpu.device, dtype=torch.float32) / 255.0
            colors = colors[:, None, None]  # shape(n,1,1,3)
            masks = masks.unsqueeze(3)  # shape(n,h,w,1)
            masks_color = masks * (colors * alpha)  # shape(n,h,w,3)

            inv_alph_masks = (1 - masks * alpha).cumprod(0)  # shape(n,h,w,1)
            mcs = (masks_color * inv_alph_masks).sum(0) * 2  # mask color summand shape(n,h,w,3)

            im_gpu = im_gpu.flip(dims=[0])  # flip channel
            im_gpu = im_gpu.permute(1, 2, 0).contiguous()  # shape(h,w,3)
            im_gpu = im_gpu * inv_alph_masks[-1] + mcs
            im_mask = (im_gpu * 255).byte().cpu().numpy()
            self.im[:] = scale_image(im_gpu.shape, im_mask, self.im.shape)
        if self.pil:
            # convert im back to PIL and update draw
            self.fromarray(self.im)

    def rectangle(self, xy, fill=None, outline=None, width=1):
        # Add rectangle to image (PIL-only)
        self.draw.rectangle(xy, fill, outline, width)

    def text(self, xy, text, txt_color=(255, 255, 255), anchor='top'):
        # Add text to image (PIL-only)
        if anchor == 'bottom':  # start y from font bottom
            w, h = self.font.getsize(text)  # text width, height
            xy[1] += 1 - h
        self.draw.text(xy, text, fill=txt_color, font=self.font)

    def fromarray(self, im):
        # Update self.im from a numpy array
        self.im = im if isinstance(im, Image.Image) else Image.fromarray(im)
        self.draw = ImageDraw.Draw(self.im)

    def result(self):
        # Return annotated image as array
        return np.asarray(self.im)


def feature_visualization(x, module_type, stage, n=32, save_dir=Path('runs/detect/exp')):
    """
    x:              Features to be visualized
    module_type:    Module type
    stage:          Module stage within model
    n:              Maximum number of feature maps to plot
    save_dir:       Directory to save results
    """
    if 'Detect' not in module_type:
        batch, channels, height, width = x.shape  # batch, channels, height, width
        if height > 1 and width > 1:
            f = save_dir / f"stage{stage}_{module_type.split('.')[-1]}_features.png"  # filename

            blocks = torch.chunk(x[0].cpu(), channels, dim=0)  # select batch index 0, block by channels
            n = min(n, channels)  # number of plots
            fig, ax = plt.subplots(math.ceil(n / 8), 8, tight_layout=True)  # 8 rows x n/8 cols
            ax = ax.ravel()
            plt.subplots_adjust(wspace=0.05, hspace=0.05)
            for i in range(n):
                ax[i].imshow(blocks[i].squeeze())  # cmap='gray'
                ax[i].axis('off')

            LOGGER.info(f'Saving {f}... ({n}/{channels})')
            plt.savefig(f, dpi=300, bbox_inches='tight')
            plt.close()
            np.save(str(f.with_suffix('.npy')), x[0].cpu().numpy())  # npy save


def hist2d(x, y, n=100):
    # 2d histogram used in labels.png and evolve.png
    xedges, yedges = np.linspace(x.min(), x.max(), n), np.linspace(y.min(), y.max(), n)
    hist, xedges, yedges = np.histogram2d(x, y, (xedges, yedges))
    xidx = np.clip(np.digitize(x, xedges) - 1, 0, hist.shape[0] - 1)
    yidx = np.clip(np.digitize(y, yedges) - 1, 0, hist.shape[1] - 1)
    return np.log(hist[xidx, yidx])


def butter_lowpass_filtfilt(data, cutoff=1500, fs=50000, order=5):
    from scipy.signal import butter, filtfilt

    # https://stackoverflow.com/questions/28536191/how-to-filter-smooth-with-scipy-numpy
    def butter_lowpass(cutoff, fs, order):
        nyq = 0.5 * fs
        normal_cutoff = cutoff / nyq
        return butter(order, normal_cutoff, btype='low', analog=False)

    b, a = butter_lowpass(cutoff, fs, order=order)
    return filtfilt(b, a, data)  # forward-backward filter


def output_to_target(output, max_det=300):
    # Convert model output to target format [batch_id, class_id, x, y, w, h, conf] for plotting
    targets = []
    for i, o in enumerate(output):
        box, conf, cls = o[:max_det, :6].cpu().split((4, 1, 1), 1)
        j = torch.full((conf.shape[0], 1), i)
        targets.append(torch.cat((j, cls, xyxy2xywh(box), conf), 1))
    return torch.cat(targets, 0).numpy()


@threaded
def plot_images(images, targets, paths=None, fname='images.jpg', names=None):
    # Plot image grid with labels
    if isinstance(images, torch.Tensor):
        images = images.cpu().float().numpy()
    if isinstance(targets, torch.Tensor):
        targets = targets.cpu().numpy()

    max_size = 1920  # max image size
    max_subplots = 16  # max image subplots, i.e. 4x4
    bs, _, h, w = images.shape  # batch size, _, height, width
    bs = min(bs, max_subplots)  # limit plot images
    ns = np.ceil(bs ** 0.5)  # number of subplots (square)
    if np.max(images[0]) <= 1:
        images *= 255  # de-normalise (optional)

    # Build Image
    mosaic = np.full((int(ns * h), int(ns * w), 3), 255, dtype=np.uint8)  # init
    for i, im in enumerate(images):
        if i == max_subplots:  # if last batch has fewer images than we expect
            break
        x, y = int(w * (i // ns)), int(h * (i % ns))  # block origin
        im = im.transpose(1, 2, 0)
        mosaic[y:y + h, x:x + w, :] = im

    # Resize (optional)
    scale = max_size / ns / max(h, w)
    if scale < 1:
        h = math.ceil(scale * h)
        w = math.ceil(scale * w)
        mosaic = cv2.resize(mosaic, tuple(int(x * ns) for x in (w, h)))

    # Annotate
    fs = int((h + w) * ns * 0.01)  # font size
    annotator = Annotator(mosaic, line_width=round(fs / 10), font_size=fs, pil=True, example=names)
    for i in range(i + 1):
        x, y = int(w * (i // ns)), int(h * (i % ns))  # block origin
        annotator.rectangle([x, y, x + w, y + h], None, (255, 255, 255), width=2)  # borders
        if paths:
            annotator.text((x + 5, y + 5), text=Path(paths[i]).name[:40], txt_color=(220, 220, 220))  # filenames
        if len(targets) > 0:
            ti = targets[targets[:, 0] == i]  # image targets
            boxes = xywh2xyxy(ti[:, 2:6]).T
            classes = ti[:, 1].astype('int')
            labels = ti.shape[1] == 6  # labels if no conf column
            conf = None if labels else ti[:, 6]  # check for confidence presence (label vs pred)

            if boxes.shape[1]:
                if boxes.max() <= 1.01:  # if normalized with tolerance 0.01
                    boxes[[0, 2]] *= w  # scale to pixels
                    boxes[[1, 3]] *= h
                elif scale < 1:  # absolute coords need scale if image scales
                    boxes *= scale
            boxes[[0, 2]] += x
            boxes[[1, 3]] += y
            for j, box in enumerate(boxes.T.tolist()):
                cls = classes[j]
                color = colors(cls)
                cls = names[cls] if names else cls
                if labels or conf[j] > 0.25:  # 0.25 conf thresh
                    label = f'{cls}' if labels else f'{cls} {conf[j]:.1f}'
                    annotator.box_label(box, label, color=color)
    annotator.im.save(fname)  # save


def plot_lr_scheduler(optimizer, scheduler, epochs=300, save_dir=''):
    # Plot LR simulating training for full epochs
    optimizer, scheduler = copy(optimizer), copy(scheduler)  # do not modify originals
    y = []
    for _ in range(epochs):
        scheduler.step()
        y.append(optimizer.param_groups[0]['lr'])
    plt.plot(y, '.-', label='LR')
    plt.xlabel('epoch')
    plt.ylabel('LR')
    plt.grid()
    plt.xlim(0, epochs)
    plt.ylim(0)
    plt.savefig(Path(save_dir) / 'LR.png', dpi=200)
    plt.close()


def plot_val_txt():  # from utils.plots import *; plot_val()
    # Plot val.txt histograms
    x = np.loadtxt('val.txt', dtype=np.float32)
    box = xyxy2xywh(x[:, :4])
    cx, cy = box[:, 0], box[:, 1]

    fig, ax = plt.subplots(1, 1, figsize=(6, 6), tight_layout=True)
    ax.hist2d(cx, cy, bins=600, cmax=10, cmin=0)
    ax.set_aspect('equal')
    plt.savefig('hist2d.png', dpi=300)

    fig, ax = plt.subplots(1, 2, figsize=(12, 6), tight_layout=True)
    ax[0].hist(cx, bins=600)
    ax[1].hist(cy, bins=600)
    plt.savefig('hist1d.png', dpi=200)


def plot_targets_txt():  # from utils.plots import *; plot_targets_txt()
    # Plot targets.txt histograms
    x = np.loadtxt('targets.txt', dtype=np.float32).T
    s = ['x targets', 'y targets', 'width targets', 'height targets']
    fig, ax = plt.subplots(2, 2, figsize=(8, 8), tight_layout=True)
    ax = ax.ravel()
    for i in range(4):
        ax[i].hist(x[i], bins=100, label=f'{x[i].mean():.3g} +/- {x[i].std():.3g}')
        ax[i].legend()
        ax[i].set_title(s[i])
    plt.savefig('targets.jpg', dpi=200)


def plot_val_study(file='', dir='', x=None):  # from utils.plots import *; plot_val_study()
    # Plot file=study.txt generated by val.py (or plot all study*.txt in dir)
    save_dir = Path(file).parent if file else Path(dir)
    plot2 = False  # plot additional results
    if plot2:
        ax = plt.subplots(2, 4, figsize=(10, 6), tight_layout=True)[1].ravel()

    fig2, ax2 = plt.subplots(1, 1, figsize=(8, 4), tight_layout=True)
    # for f in [save_dir / f'study_coco_{x}.txt' for x in ['yolov5n6', 'yolov5s6', 'yolov5m6', 'yolov5l6', 'yolov5x6']]:
    for f in sorted(save_dir.glob('study*.txt')):
        y = np.loadtxt(f, dtype=np.float32, usecols=[0, 1, 2, 3, 7, 8, 9], ndmin=2).T
        x = np.arange(y.shape[1]) if x is None else np.array(x)
        if plot2:
            s = ['P', 'R', 'mAP@.5', 'mAP@.5:.95', 't_preprocess (ms/img)', 't_inference (ms/img)', 't_NMS (ms/img)']
            for i in range(7):
                ax[i].plot(x, y[i], '.-', linewidth=2, markersize=8)
                ax[i].set_title(s[i])

        j = y[3].argmax() + 1
        ax2.plot(y[5, 1:j],
                 y[3, 1:j] * 1E2,
                 '.-',
                 linewidth=2,
                 markersize=8,
                 label=f.stem.replace('study_coco_', '').replace('yolo', 'YOLO'))

    ax2.plot(1E3 / np.array([209, 140, 97, 58, 35, 18]), [34.6, 40.5, 43.0, 47.5, 49.7, 51.5],
             'k.-',
             linewidth=2,
             markersize=8,
             alpha=.25,
             label='EfficientDet')

    ax2.grid(alpha=0.2)
    ax2.set_yticks(np.arange(20, 60, 5))
    ax2.set_xlim(0, 57)
    ax2.set_ylim(25, 55)
    ax2.set_xlabel('GPU Speed (ms/img)')
    ax2.set_ylabel('COCO AP val')
    ax2.legend(loc='lower right')
    f = save_dir / 'study.png'
    print(f'Saving {f}...')
    plt.savefig(f, dpi=300)


@TryExcept()  # known issue https://github.com/ultralytics/yolov5/issues/5395
def plot_labels(labels, names=(), save_dir=Path('')):
    # plot dataset labels
    LOGGER.info(f"Plotting labels to {save_dir / 'labels.jpg'}... ")
    c, b = labels[:, 0], labels[:, 1:].transpose()  # classes, boxes
    nc = int(c.max() + 1)  # number of classes
    x = pd.DataFrame(b.transpose(), columns=['x', 'y', 'width', 'height'])

    # seaborn correlogram
    sn.pairplot(x, corner=True, diag_kind='auto', kind='hist', diag_kws=dict(bins=50), plot_kws=dict(pmax=0.9))
    plt.savefig(save_dir / 'labels_correlogram.jpg', dpi=200)
    plt.close()

    # matplotlib labels
    matplotlib.use('svg')  # faster
    ax = plt.subplots(2, 2, figsize=(8, 8), tight_layout=True)[1].ravel()
    y = ax[0].hist(c, bins=np.linspace(0, nc, nc + 1) - 0.5, rwidth=0.8)
    with contextlib.suppress(Exception):  # color histogram bars by class
        [y[2].patches[i].set_color([x / 255 for x in colors(i)]) for i in range(nc)]  # known issue #3195
    ax[0].set_ylabel('instances')
    if 0 < len(names) < 30:
        ax[0].set_xticks(range(len(names)))
        ax[0].set_xticklabels(list(names.values()), rotation=90, fontsize=10)
    else:
        ax[0].set_xlabel('classes')
    sn.histplot(x, x='x', y='y', ax=ax[2], bins=50, pmax=0.9)
    sn.histplot(x, x='width', y='height', ax=ax[3], bins=50, pmax=0.9)

    # rectangles
    labels[:, 1:3] = 0.5  # center
    labels[:, 1:] = xywh2xyxy(labels[:, 1:]) * 2000
    img = Image.fromarray(np.ones((2000, 2000, 3), dtype=np.uint8) * 255)
    for cls, *box in labels[:1000]:
        ImageDraw.Draw(img).rectangle(box, width=1, outline=colors(cls))  # plot
    ax[1].imshow(img)
    ax[1].axis('off')

    for a in [0, 1, 2, 3]:
        for s in ['top', 'right', 'left', 'bottom']:
            ax[a].spines[s].set_visible(False)

    plt.savefig(save_dir / 'labels.jpg', dpi=200)
    matplotlib.use('Agg')
    plt.close()


# REVIEW: add test_cls to make test gt right

def imshow_cls(im, labels=None, pred=None, test_cls=None, names=None, nmax=25, verbose=False, f=Path('images.jpg')):
    # Show classification image grid with labels (optional) and predictions (optional)
    from utils.augmentations import denormalize
    names = names or [f'class{i}' for i in range(1000)]
    blocks = torch.chunk(denormalize(im.clone()).cpu().float(), len(im),
                         dim=0)  # select batch index 0, block by channels
    n = min(len(blocks), nmax)  # number of plots
    m = min(8, round(n ** 0.5))  # 8 x 8 default
    fig, ax = plt.subplots(math.ceil(n / m), m)  # 8 rows x n/8 cols
    ax = ax.ravel() if m > 1 else [ax]
    # plt.subplots_adjust(wspace=0.05, hspace=0.05)
    for i in range(n):
        ax[i].imshow(blocks[i].squeeze().permute((1, 2, 0)).numpy().clip(0.0, 1.0))
        ax[i].axis('off')
        if labels is not None:
            # REVIEW: Turn tensors into np.array
            labels = np.array( labels )
            
            # REVIEW: cahnge test_images output 
            if test_cls and pred is not None:
                s =  "gt:" + test_cls[labels[i]] + (f', pred:{names[pred.tolist()[i]]}' if pred is not None else '')
            
            elif pred is not None:
                s =  "gt: " + str(labels[i]) + ' pred: ' + str(pred[i])
            else:
                #print( names,labels[i] )
                s =  "gt:" + names[labels[i]]
                
            ax[i].set_title(s, fontsize=8, verticalalignment='top')
    plt.savefig(f, dpi=300, bbox_inches='tight')
    plt.close()
    if verbose:
        LOGGER.info(f"Saving {f}")
        if labels is not None:
            LOGGER.info('True:     ' + ' '.join(f'{names[i]:3s}' for i in labels[:nmax]))
        if pred is not None:
            LOGGER.info('Predicted:' + ' '.join(f'{names[i]:3s}' for i in pred[:nmax]))
    return f


def plot_evolve(evolve_csv='path/to/evolve.csv'):  # from utils.plots import *; plot_evolve()
    # Plot evolve.csv hyp evolution results
    evolve_csv = Path(evolve_csv)
    data = pd.read_csv(evolve_csv)
    keys = [x.strip() for x in data.columns]
    x = data.values
    f = fitness(x)
    j = np.argmax(f)  # max fitness index
    plt.figure(figsize=(10, 12), tight_layout=True)
    matplotlib.rc('font', **{'size': 8})
    print(f'Best results from row {j} of {evolve_csv}:')
    for i, k in enumerate(keys[7:]):
        v = x[:, 7 + i]
        mu = v[j]  # best single result
        plt.subplot(6, 5, i + 1)
        plt.scatter(v, f, c=hist2d(v, f, 20), cmap='viridis', alpha=.8, edgecolors='none')
        plt.plot(mu, f.max(), 'k+', markersize=15)
        plt.title(f'{k} = {mu:.3g}', fontdict={'size': 9})  # limit to 40 characters
        if i % 5 != 0:
            plt.yticks([])
        print(f'{k:>15}: {mu:.3g}')
    f = evolve_csv.with_suffix('.png')  # filename
    plt.savefig(f, dpi=200)
    plt.close()
    print(f'Saved {f}')


def plot_results(file='path/to/results.csv', dir=''):
    # Plot training results.csv. Usage: from utils.plots import *; plot_results('path/to/results.csv')
    save_dir = Path(file).parent if file else Path(dir)
    fig, ax = plt.subplots(2, 5, figsize=(12, 6), tight_layout=True)
    ax = ax.ravel()
    files = list(save_dir.glob('results*.csv'))
    assert len(files), f'No results.csv files found in {save_dir.resolve()}, nothing to plot.'
    for f in files:
        try:
            data = pd.read_csv(f)
            s = [x.strip() for x in data.columns]
            x = data.values[:, 0]
            for i, j in enumerate([1, 2, 3, 4, 5, 8, 9, 10, 6, 7]):
                y = data.values[:, j].astype('float')
                # y[y == 0] = np.nan  # don't show zero values
                ax[i].plot(x, y, marker='.', label=f.stem, linewidth=2, markersize=8)
                ax[i].set_title(s[j], fontsize=12)
                # if j in [8, 9, 10]:  # share train and val loss y axes
                #     ax[i].get_shared_y_axes().join(ax[i], ax[i - 5])
        except Exception as e:
            LOGGER.info(f'Warning: Plotting error for {f}: {e}')
    ax[1].legend()
    fig.savefig(save_dir / 'results.png', dpi=200)
    plt.close()


def profile_idetection(start=0, stop=0, labels=(), save_dir=''):
    # Plot iDetection '*.txt' per-image logs. from utils.plots import *; profile_idetection()
    ax = plt.subplots(2, 4, figsize=(12, 6), tight_layout=True)[1].ravel()
    s = ['Images', 'Free Storage (GB)', 'RAM Usage (GB)', 'Battery', 'dt_raw (ms)', 'dt_smooth (ms)', 'real-world FPS']
    files = list(Path(save_dir).glob('frames*.txt'))
    for fi, f in enumerate(files):
        try:
            results = np.loadtxt(f, ndmin=2).T[:, 90:-30]  # clip first and last rows
            n = results.shape[1]  # number of rows
            x = np.arange(start, min(stop, n) if stop else n)
            results = results[:, x]
            t = (results[0] - results[0].min())  # set t0=0s
            results[0] = x
            for i, a in enumerate(ax):
                if i < len(results):
                    label = labels[fi] if len(labels) else f.stem.replace('frames_', '')
                    a.plot(t, results[i], marker='.', label=label, linewidth=1, markersize=5)
                    a.set_title(s[i])
                    a.set_xlabel('time (s)')
                    # if fi == len(files) - 1:
                    #     a.set_ylim(bottom=0)
                    for side in ['top', 'right']:
                        a.spines[side].set_visible(False)
                else:
                    a.remove()
        except Exception as e:
            print(f'Warning: Plotting error for {f}; {e}')
    ax[1].legend()
    plt.savefig(Path(save_dir) / 'idetection_profile.png', dpi=200)


def save_one_box(xyxy, im, file=Path('im.jpg'), gain=1.02, pad=10, square=False, BGR=False, save=True):
    # Save image crop as {file} with crop size multiple {gain} and {pad} pixels. Save and/or return crop
    xyxy = torch.tensor(xyxy).view(-1, 4)
    b = xyxy2xywh(xyxy)  # boxes
    if square:
        b[:, 2:] = b[:, 2:].max(1)[0].unsqueeze(1)  # attempt rectangle to square
    b[:, 2:] = b[:, 2:] * gain + pad  # box wh * gain + pad
    xyxy = xywh2xyxy(b).long()
    clip_boxes(xyxy, im.shape)
    crop = im[int(xyxy[0, 1]):int(xyxy[0, 3]), int(xyxy[0, 0]):int(xyxy[0, 2]), ::(1 if BGR else -1)]
    if save:
        file.parent.mkdir(parents=True, exist_ok=True)  # make directory
        f = str(increment_path(file).with_suffix('.jpg'))
        # cv2.imwrite(f, crop)  # save BGR, https://github.com/ultralytics/yolov5/issues/7007 chroma subsampling issue
        Image.fromarray(crop[..., ::-1]).save(f, quality=95, subsampling=0)  # save RGB
    return crop

# REVIEW: add write report function
def WriteReport(target, pred, save_dir, classes, mode):
    classes = list(classes)
    confusion_matrix = ms.confusion_matrix( y_true=target.cpu().numpy(), y_pred=pred.cpu().numpy(), labels=list(map(int, np.unique(classes))) ) 
    cls_report = ms.classification_report(target.cpu().numpy(), pred.cpu().numpy(), zero_division=0)
    
    with open( os.path.join(save_dir, mode + "_cls_report.txt"), 'w') as f:
        f.write( cls_report )

    with open( os.path.join(save_dir, mode + "_confision_matrix.txt"), 'w') as f:
        f.write("\t" + "|" + "\t" )
        for i in classes :
            f.write( str(i) + "\t")
        f.write( "\n" )

        for i in range( len( classes ) ) :
            f.write( "-----" )
        f.write( "\n" )

        for i, row in enumerate(confusion_matrix):
            f.write( str(classes[i]) + "\t" + "|" + "\t")
            for j in row:
                f.write(np.array2string( j ) + "\t")
            f.write( "\n" )
            
def Plot_Prob_Distribution( pred_prob, gt_label, path, epoch) :
    plt.figure(figsize=(12,12))
    for i in range(1, 5):
        pred = pred_prob[i][:].tolist()
        label = gt_label[i].tolist()
        label_range = np.arange(0, 360)
        text_position = max( pred ) + 1
        bar_length = max( pred ) - min( pred )
        plt.subplot( 2, 2, i )
        plt.bar( label, bar_length, bottom=min( pred ), color='blue', width=4 )
        plt.ylabel('Value', fontsize=10)
        plt.xlabel('Angle', fontsize=10)
        plt.title('gt: '+ str(label) + ' pred: ' + str(pred.index(max(pred))), fontsize=10)
        plt.subplots_adjust(left=0.125,
                    bottom=0.1, 
                    right=0.9, 
                    top=0.9, 
                    wspace=0.2, 
                    hspace=0.35)
        plt.plot( label_range, pred, 'r-')
    plt.savefig( os.path.join( path, ( 'prob_dis_epoch' + str(epoch) ) ) )
    plt.close()

def Plot_Prob_Distribution_Large_Bias( pred_prob, gt_label, path, epoch ) :
    preds = pred_prob[0].tolist()
    preds_post = pred_prob[1].tolist()
    img_list = pred_prob[2]
    labels = gt_label.tolist()
    biases = []
    imshow_img , imshow_label, imshow_pred = [], [], []
    
    for i, label in enumerate( labels ):
        post = abs(np.argmax(preds_post[i])-label)
        ori = abs(np.argmax(preds[i])-label)
        if post > 180:
            post = 360 - post
        if ori > 180:
            ori = 360 - ori
            
        biases.append( [preds[i], label, preds_post[i]] )
        imshow_img.append( img_list[i].unsqueeze(0) )
        imshow_label.append( label )
        imshow_pred.append( preds_post[i].index(max(preds_post[i])) )
    plt.figure(figsize=(12,12))
    
    for i, bias in enumerate( biases[:12] ) :
        label_range = np.arange(0, 360)
        plt.figure(figsize=(12,12))
        plt.subplot( 2, 1, 1 )
        plt.plot(   bias[1], max(bias[0]), 'b.' )
        plt.ylabel('Value', fontsize=10)
        plt.xlabel('Angle', fontsize=10)
        plt.title('gt(blue dot): '+ str(bias[1]) + '    pred: ' + str(bias[0].index(max(bias[0]))), fontsize=10)
        plt.subplots_adjust( left=0.125,bottom=0.1, right=0.9, top=0.9, wspace=0.2, hspace=0.35 )
        plt.plot( label_range, bias[0], 'r-')
        
        plt.subplot( 2, 1, 2 )
        plt.plot(   bias[1], max(bias[2]), 'b.' )
        plt.ylabel('Value', fontsize=10)
        plt.xlabel('Angle', fontsize=10)
        plt.title('+-5 sum -- gt(blue dot): '+ str(bias[1]) + '    pred: ' + str(bias[2].index(max(bias[2]))), fontsize=10)
        plt.subplots_adjust( left=0.125,bottom=0.1, right=0.9, top=0.9, wspace=0.2, hspace=0.35 )
        plt.plot( label_range, bias[2], 'r-')
        plt.savefig( os.path.join( path, ( 'ep' + str(epoch) + '_' + str(i) ) ) )
        plt.close()
        
    # imshow_cls( torch.cat(imshow_img)[:12], imshow_label[:12], pred=imshow_pred[:12], f=os.path.join( path, ( 'ep' + str(epoch) + '_bias_img' ) ) )
        
# def Plot_Prob_Distribution_Large_Bias( pred_prob, gt_label, path, epoch ) :
#     preds = pred_prob[0].tolist()
#     preds_post = pred_prob[1].tolist()
#     labels = gt_label.tolist()
#     biases = []
#     for i, label in enumerate( labels ):
#         minus = abs(np.argmax(preds[i])-label)
#         if minus > 180:
#             minus = 360 - minus
#         if minus > 50 :
#             biases.append( [preds[i], label, preds_post[i]] )
    
#     plt.figure(figsize=(12,12))
#     for i, bias in enumerate( biases ) :
#         unplt = True
#         label_range = np.arange(0, 360)
#         AZ
#         plt.subplot( 1, 2, 1 )
#         plt.plot(   bias[1], max(bias[0]), 'b.' )
#         plt.ylabel('Value', fontsize=10)
#         plt.xlabel('Angle', fontsize=10)
#         plt.title('gt(blue dot): '+ str(bias[1]) + '    pred: ' + str(bias[0].index(max(bias[0]))), fontsize=10)
#         plt.subplots_adjust( left=0.125,bottom=0.1, right=0.9, top=0.9, wspace=0.2, hspace=0.35 )
#         plt.plot( label_range, bias[0], 'r-')
#         plt.savefig( os.path.join( path, ( 'ep' + str(epoch) + '_' + str(i) ) ) )
#         plt.close()
#         plt.figure(figsize=(12,12))
#     if unplt:
#         plt.savefig( os.path.join( path, ( 'ep' + str(epoch) + '_' + str(i) ) ) )
#         plt.close()

def Plot_Wrong_Sample_Distribution( wrong_preds, path, epoch) :
    plt.figure(figsize=(12,12))
    preds, targets = zip(*wrong_preds)
    preds = [int(pred.cpu().numpy()) for pred in preds] 
    targets = [ int(target.cpu().numpy()) for target in targets]
    label_range = np.arange(0, 360)
    plt.title('Wrong Samples Distribution', fontsize=20)
    plt.ylabel( 'Predict Angle', fontsize=15 )
    plt.xlabel( 'Ground Truth Angle', fontsize=15 )
    plt.plot( label_range, linewidth=11, color='#ffff00' )
    # plt.plot( label_range, linewidth=5 )
    plt.plot( targets, preds, 'r.')
    plt.savefig( os.path.join( path, ( 'wrong_preds_epoch' + str(epoch) ) ) )
    plt.close()
    
    
def Plot_Gt_In_Topk( preds, path, epoch ) :
    preds = [int(pred.cpu().numpy()) for pred in preds] 
    counted_list = Counter(preds)
    elements = []
    counts = []
    for elem, count in counted_list.items():
        elements.append(elem)
        counts.append(count)
    
    plt.title('GT Location', fontsize=20)
    plt.ylabel( 'Times', fontsize=10 )
    plt.xlabel( 'TopK ', fontsize=10 )
    plt.bar(elements,counts)
    plt.savefig( os.path.join( path, ( 'angle_bias_epoch' + str(epoch) ) ) )
    plt.close()
    
def Plot_Guassian( preds, targets, path, epoch ):
    pred = preds[0, :].detach().cpu().numpy()
    target = targets[0, :].detach().cpu().numpy()
    pred_max = np.argmax(pred)
    tar_max = np.argmax(target)
    label_range = np.arange(0, 360)
    plt.title('original: '+str(tar_max)+', gaussian: '+str(pred_max), fontsize=20)
    plt.ylabel( 'Value', fontsize=10 )
    plt.xlabel( 'Angle', fontsize=10 )
    plt.plot( label_range, target, 'y-')
    plt.plot( label_range, pred, 'r-' )
    
    # plt.text(50, 50, 'original: '+str(tar_max)+', gaussiaun: '+str(pred_max), fontsize=12, color='black')

    plt.savefig( os.path.join( path, ( 'gaussian_epoch' + str(epoch) ) ) )
    plt.close()
    
def Plot_Value_Different_Between_GTandPreds( wrong_values, save_func_dir, epoch ):
    targets, pred_values, target_values = [], [], []
    for value in wrong_values:
        targets.append( value[0].cpu().numpy() )
        pred_values.append( value[1].cpu().numpy() )
        target_values.append( value[2].cpu().numpy() )
        
    plt.title('Pred_Target Value', fontsize=20)
    plt.ylabel( 'Value', fontsize=10 )
    plt.xlabel( 'Angle', fontsize=10 )
    plt.plot( targets, pred_values, 'y.')
    plt.plot( targets, target_values, 'r.' )
    plt.savefig( os.path.join( save_func_dir, ( 'pred_target_epoch' + str(epoch) ) ) )
    plt.close()

def Plot_Topk_CDF( corrects, save_func_dir, epoch ):
    corrects = corrects.detach().cpu().numpy()
    topk = []
    topk_len = len(corrects[0, :]) 
    batch_size = len(corrects[:,0])
    label_range = np.arange(1, topk_len+1)
    
    for i in range(topk_len):
        acc = corrects[:, :i+1].sum() /  batch_size 
        # print( acc )
        topk.append( acc )
        
    # print( topk )
    plt.title( 'TopK_CDF' )
    plt.ylabel( 'Value', fontsize=10 )
    plt.xlabel( 'Topk', fontsize=10 )
    plt.plot( label_range, topk, 'r-')
    plt.savefig( os.path.join( save_func_dir, ( 'topk_CDF_epoch' + str(epoch) ) ) )
    plt.close()
    
def Plot_Topk_Bias_Distribution( bias_angle, save_func_dir, epoch ):
    bias_angle = bias_angle.detach().cpu().numpy()
    fig = plt.figure(figsize=(12,12))
    fig.supxlabel('TopK' )
    fig.supylabel('%')
    # fig.suptitle("Title for whole figure", fontsize=16)
    count_list =[]
    angles = [ 0, 45, 90, 135, 180, 225, 270, 315 ]
    label_range = np.arange(1, len(bias_angle[0, :])+1)
    
    for j, angle in enumerate(angles):
        topk_count = np.zeros(15, dtype=int)
        
        for topk_bias in bias_angle:
            for i, bias in enumerate(topk_bias):
                if angle == 0 :
                    if bias >= 360 or ( bias != 0 and bias <= 10 ):
                        topk_count[i] += 1
                elif bias >= angle-10 and bias <= angle+10:
                    topk_count[i] += 1
        # topk_count = topk_count.astype(float) / len(bias_angle[:, 0]) * 100
        count_list.append( topk_count )

    for i, angle in enumerate(angles):
        percent_list = [ float(x)/np.sum( np.array(count_list), axis=0 )[j]*100 for j, x in enumerate(count_list[i]) ]
        ax = fig.add_subplot(241+i)
        ax.set_title( str(angle-10) + '~' + str(angle+10) )
        ax.bar(label_range, percent_list)
        ax.set_xlim([0, len(bias_angle[0, :])+1])
        ax.set_ylim([0, 100])
    
    plt.savefig( os.path.join( save_func_dir, 'epoch'+ str(epoch) )  )

def Plot_Bias_Top1_CDF( bias_list, save_func_dir, epoch ):
    counter_list = []
    if bias_list[0] != None:
        bias_median = [ int(b.cpu().numpy()) for b in bias_list[0] ]
        counter_list.append( Counter(bias_median) )
    bias_ori = [ int(b.cpu().numpy()) for b in bias_list[1] ]
    counter_list.append( Counter(bias_ori) )

    for i in range(len(counter_list)):
        elements = []
        counts = []
        counts_dict = collections.OrderedDict(sorted(counter_list[i].items()))
        for elem, count in counts_dict.items():
            elements.append( elem )
            counts.append( count )

        counts = [ float( sum(counts[:j+1]))  / sum(counts) * 100 for j, count in enumerate(counts) ]

        plt.title( 'Bias_CDF, { blue:origin, red:median }' )
        plt.ylabel( '%', fontsize=10 )
        plt.xlabel( 'Angle', fontsize=10 )
        if i == 0:
            plt.plot( elements, counts, 'r-' )
        else:
            plt.plot( elements, counts, 'b-' )
    plt.savefig( os.path.join( save_func_dir, 'epoch'+ str( epoch ) )  )
    plt.close()
    
    
    # fig = plt.figure(figsize=(10,10))
    # fig.supxlabel('angle' )
    # fig.supylabel('%')
    
    # for i in range(2):
    #     elements = []
    #     counts = []
    #     for elem, count in counter_list[i].items():
    #         elements.append( elem )
    #         counts.append( count )
    #     counts = [ float(count) / sum(counts) * 100 for count in counts ]
        
    #     ax = fig.add_subplot( 211+i )
    #     ax.set_title( 'median filter bias' if i == 0 else 'original bias' )
    #     ax.bar( elements, counts )
    #     ax.set_xlim([0, 180])
    #     ax.set_ylim([0, 100])
    # plt.savefig( os.path.join( save_func_dir, 'epoch'+ str( epoch ) )  )

def Plot_Topk_Threshold( topk_list, save_func_dir, epoch ):
    label_range = np.arange(0, len(topk_list))
    top1_list = np.array(topk_list)[:, 0]
    top15_list = np.array(topk_list)[:, 1]
    plt.figure(figsize=(12,12))
    
    ax = plt.subplot( 2, 1, 1 )
    ax.set_ylim([0, 1])
    plt.ylabel( 'Accuracy', fontsize=10 )
    plt.xlabel( 'Bias', fontsize=10 )
    plt.title( 'Top1', fontsize=10 )
    plt.subplots_adjust( left=0.125,bottom=0.1, right=0.9, top=0.9, wspace=0.2, hspace=0.35 )
    plt.plot( label_range, top1_list, 'r-')
    
    ax = plt.subplot( 2, 1, 2 )
    ax.set_ylim([0, 1])
    plt.ylabel( 'Accuracy', fontsize=10 )
    plt.xlabel( 'Bias', fontsize=10 )
    plt.title( 'Top15', fontsize=10 )
    plt.subplots_adjust( left=0.125,bottom=0.1, right=0.9, top=0.9, wspace=0.2, hspace=0.35 )
    plt.plot( label_range, top15_list, 'r-')
   
    plt.savefig( os.path.join( save_func_dir, ( 'ep' + str(epoch) ) ) ) 
    plt.close()

def Plot_What_U_Want( func_name, save_dir, epoch, preds=None, targets=None ):
    layer = ['51']
    save_func_dir = os.path.join( save_dir, func_name )
    if not os.path.exists( save_func_dir ):
        # REVIEW: 3layer
        # os.mkdir( os.path.join( save_dir, func_name ) )
        # for i in range(len(layer)) :
        #     save_func_dir = os.path.join( save_dir, func_name, layer[i] )
        os.mkdir( save_func_dir )
    
    if func_name == 'prob_dis':
        # Plot_Prob_Distribution( preds[:, (i)*360:(i+1)*360 ], targets, layer_dir, epoch )
        Plot_Prob_Distribution( preds, targets, save_func_dir, epoch )
    elif func_name == 'wrong_dis':
        # Plot_Wrong_Sample_Distribution( preds[i], layer_dir, epoch )
        Plot_Wrong_Sample_Distribution( preds, save_func_dir, epoch )
    elif func_name == 'gt_loc':
        Plot_Gt_In_Topk( preds, save_func_dir, epoch )
    elif func_name == 'gaussian':
        Plot_Guassian( preds, targets,save_func_dir, epoch )
    elif func_name == 'topk_cdf':
        Plot_Topk_CDF( preds, save_func_dir, epoch )
    elif func_name == 'bias_topk':
        Plot_Topk_Bias_Distribution( preds, save_func_dir, epoch )
    elif func_name == 'bias_mid_top1':
        Plot_Bias_Top1_CDF( bias_list=preds, save_func_dir=save_func_dir, epoch=epoch )
    elif func_name == 'prob_dis_bias' :
        Plot_Prob_Distribution_Large_Bias( preds, targets, save_func_dir, epoch )
    elif func_name == 'topk_threshold' :
        Plot_Topk_Threshold( preds, save_func_dir, epoch )
    # elif func_name == 'value_difference':
    #     Plot_Value_Different( preds, save_func_dir, epoch )
