import math
import os
from copy import deepcopy
import numpy as np
import torch
import pickle
from monai import data, transforms
from monai.data import *
from monai.transforms import *
from .TumorGenerated import TumorGenerated
from torch.utils.data import DataLoader, ConcatDataset


class Sampler(torch.utils.data.Sampler):
    def __init__(self, dataset, num_replicas=None, rank=None, shuffle=True, make_even=True):
        if num_replicas is None:
            if not torch.distributed.is_available():
                raise RuntimeError("Requires distributed package to be available")
            num_replicas = torch.distributed.get_world_size()
        if rank is None:
            if not torch.distributed.is_available():
                raise RuntimeError("Requires distributed package to be available")
            rank = torch.distributed.get_rank()
        self.shuffle = shuffle
        self.make_even = make_even
        self.dataset = dataset
        self.num_replicas = num_replicas
        self.rank = rank
        self.epoch = 0
        self.num_samples = int(math.ceil(len(self.dataset) * 1.0 / self.num_replicas))
        self.total_size = self.num_samples * self.num_replicas
        indices = list(range(len(self.dataset)))
        self.valid_length = len(indices[self.rank: self.total_size: self.num_replicas])

    def __iter__(self):
        if self.shuffle:
            g = torch.Generator()
            g.manual_seed(self.epoch)
            indices = torch.randperm(len(self.dataset), generator=g).tolist()
        else:
            indices = list(range(len(self.dataset)))
        if self.make_even:
            if len(indices) < self.total_size:
                if self.total_size - len(indices) < len(indices):
                    indices += indices[: (self.total_size - len(indices))]
                else:
                    extra_ids = np.random.randint(low=0, high=len(indices), size=self.total_size - len(indices))
                    indices += [indices[ids] for ids in extra_ids]
            assert len(indices) == self.total_size
        indices = indices[self.rank: self.total_size: self.num_replicas]
        self.num_samples = len(indices)
        return iter(indices)

    def __len__(self):
        return self.num_samples

    def set_epoch(self, epoch):
        self.epoch = epoch


def get_trans(args):
    base_trans = [
        LoadImaged(keys=["image", "label"]),
        EnsureChannelFirstd(keys=["image", "label"]),
        Orientationd(keys=["image", "label"], axcodes="RAS"),
        Spacingd(keys=["image", "label"], pixdim=(args.space_x, args.space_y, args.space_z),
                 mode=("bilinear", "nearest")),
        ScaleIntensityRanged(
            keys=["image"],
            a_min=args.a_min,
            a_max=args.a_max,
            b_min=0.0,
            b_max=1.0,
            clip=True,
        ),
        CropForegroundd(keys=["image", "label"], source_key="image"),
        SpatialPadd(keys=["image", "label"], spatial_size=(args.roi_x, args.roi_y, args.roi_z),
                    mode='constant'),
        transforms.RandShiftIntensityd(keys="image", offsets=0.1, prob=0),

    ]

    random_trans = [
        SpatialPadd(keys=["image", "label"], spatial_size=(args.roi_x, args.roi_y, args.roi_z),
                    mode='constant'),
        RandCropByPosNegLabeld(
            keys=["image", "label"],
            label_key="label",
            spatial_size=(args.roi_x, args.roi_y, args.roi_z),
            pos=args.pos,
            neg=args.neg,
            num_samples=args.sw_batch_size,
            image_key="image",
            image_threshold=0,
        ),
        transforms.RandFlipd(keys=["image", "label"], prob=args.RandFlipd_prob, spatial_axis=0),
        transforms.RandFlipd(keys=["image", "label"], prob=args.RandFlipd_prob, spatial_axis=1),
        transforms.RandFlipd(keys=["image", "label"], prob=args.RandFlipd_prob, spatial_axis=2),
        transforms.RandRotate90d(keys=["image", "label"], prob=args.RandRotate90d_prob, max_k=3),
        transforms.RandScaleIntensityd(keys="image", factors=0.1, prob=args.RandScaleIntensityd_prob),
        transforms.RandShiftIntensityd(keys="image", offsets=0.1, prob=args.RandShiftIntensityd_prob),

    ]
    return base_trans, random_trans


class Filter_KiTs_Labels(MapTransform):
    """Filter unsed label.
    """

    def __call__(self, data):
        d = dict(data)
        for key in self.keys:
            new_img = d[key].clone()
            new_img[d[key] == 3] = 1
            d[key] = new_img.float()

        return d


# liver: 1, liver tumor: 2,  pancreas: 3, pancreas tumor: 4   kidney: 5,  kidney tumor: 6
class Filter_LITS_alltraining_Labels(MapTransform):
    """Filter unsed label.
    """

    def __call__(self, data):
        d = dict(data)
        for key in self.keys:
            new_img = d[key].clone()
            # trans tumor
            new_img[d[key] == 1] = 1
            new_img[d[key] == 2] = 2
            d[key] = new_img.float()

        return d


# liver: 1, liver tumor: 2,  pancreas: 3, pancreas tumor: 4   kidney: 5,  kidney tumor: 6
class Filter_PANC_alltraining_Labels(MapTransform):
    """Filter unsed label.
    """

    def __call__(self, data):
        d = dict(data)
        for key in self.keys:
            new_img = d[key].clone()
            # trans pancreas
            new_img[d[key] == 1] = 3
            # trans tumor
            new_img[d[key] == 2] = 4
            d[key] = new_img.float()

        return d


# liver: 1, liver tumor: 2,  pancreas: 3, pancreas tumor: 4   kidney: 5,  kidney tumor: 6
class Filter_KITS_alltraining_Labels(MapTransform):
    """Filter unsed label.
    """

    def __call__(self, data):
        d = dict(data)
        for key in self.keys:
            new_img = d[key].clone()
            # trans kits
            new_img[d[key] == 1] = 5
            # trans tumor
            new_img[d[key] == 2] = 6
            d[key] = new_img.float()

        return d


class Filter_to_liver(MapTransform):
    """Filter unsed label.
    """
    def __call__(self, data):
        d = dict(data)

        for key in self.keys:
            new_img = d[key].clone()
            # liver: 1, liver tumor: 2,  pancreas: 3, pancreas tumor: 4   kidney: 5,  kidney tumor: 6
            # Transform to: liver: 1, liver tumor: 2
            new_img[d[key] > 2] = 0
            d[key] = new_img.float()

        return d


class Filter_to_panc(MapTransform):
    """Filter unsed label.
    """
    def __call__(self, data):
        d = dict(data)

        for key in self.keys:
            new_img = d[key].clone()
            # liver: 1, liver tumor: 2,  pancreas: 3, pancreas tumor: 4   kidney: 5,  kidney tumor: 6
            # Transform to: pancreas: 1, pancreas tumor: 2
            new_img[d[key] == 3] = 1
            new_img[d[key] == 4] = 2
            new_img[d[key] < 3] = 0
            new_img[d[key] > 4] = 0

            d[key] = new_img.float()

        return d


class Filter_to_kidney(MapTransform):
    """Filter unsed label.
    """
    def __call__(self, data):
        d = dict(data)
        for key in self.keys:
            new_img = d[key].clone()
            # liver: 1, liver tumor: 2,  pancreas: 3, pancreas tumor: 4   kidney: 5,  kidney tumor: 6
            # Transform to: kidney: 1,  kidney tumor: 2

            new_img[d[key] == 5] = 1
            new_img[d[key] == 6] = 2
            new_img[d[key] < 5] = 0

            d[key] = new_img.float()

        return d
