#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
from datetime import datetime
import os, glob, gc, re
import argparse, argcomplete
import numpy as np
from tqdm import tqdm
import tensorflow as tf
from tensorflow import keras
from tensorflow.keras import optimizers, losses, metrics, callbacks
from tensorflow.keras.mixed_precision import experimental as mixed_precision

from train import SparseMeanIoU
from models.UNet import unet_model_zero_pad
from utils.dataset import generate_predict_dataset, reconstruct_predicted_masks, \
        save_predicted_masks, BrainSegPredictSequence

def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
            formatter_class=argparse.RawDescriptionHelpFormatter,
            description='Predicting\n\t')

    ckpt_parser = parser.add_argument_group(
            'Model checkpoint configurations')
    ckpt_parser.add_argument("ckpt_filepath", type=str, 
            help="Checkpoint filepath to load and resume predicting from "
            "e.g. ./cp-001-50.51.ckpt.index")
    ckpt_parser.add_argument("--ckpt-weights-only", action='store_true',
            help="Checkpoints will only save the model weights (Default: False)")
    ckpt_parser.add_argument('--model', choices=['UNet'],
            help="Network model used for predicting")

    dataset_parser = parser.add_argument_group(
            'Dataset configurations')
    dataset_parser.add_argument("svs_dirs", type=str, nargs='+',
            help="Directories of svs files (e.g. data/box_Ab data/box_control)")
    dataset_parser.add_argument("--patch-size", type=int, default=1024,
            help="Patch size (Default: 1024)")

    predict_parser = parser.add_argument_group(
            'Predicting configurations')
    predict_parser.add_argument("--batch-size", type=int, default=32,
            help="Batch size of patches")
    predict_parser.add_argument("--save-dir", type=str, 
            default="/BrainSeg/data/outputs",
            help="Output directory (Default: /BrainSeg/data/outputs/model_name)")

    return parser

def predict(args):
    # Check if GPU is available
    print("Num GPUs Available: %d", len(tf.config.list_physical_devices('GPU')))

    # Set tf.keras mixed precision to float16
    policy = mixed_precision.Policy('mixed_float16')
    mixed_precision.set_policy(policy)

    # Create dataset
    svs_paths, patch_dir \
            = generate_predict_dataset(args.svs_dirs, args.patch_size)

    # Create network model
    if args.ckpt_weights_only:
        if args.model == 'UNet':
            model = unet_model_zero_pad(output_channels=3)
        model.load_weights(args.ckpt_filepath).assert_existing_objects_matched()
        print('Model weights loaded')
    else:
        model = keras.models.load_model(args.ckpt_filepath, 
                custom_objects={'SparseMeanIoU': SparseMeanIoU})
        print('Full model (weights + optimizer state) loaded')

    # Create output directory
    args.save_dir = os.path.join(os.path.abspath(args.save_dir), model.name)
    if not os.path.exists(args.save_dir):
        os.makedirs(args.save_dir)
    print(f'\tSaving predicted mask to "{args.save_dir}"')

    success_count = 0
    for i, svs_path in enumerate(tqdm(svs_paths)):
        svs_name = svs_path.split('/')[-1].replace('.svs', '')

        # Load corresponding patches
        svs_patch_dir = os.path.join(patch_dir, 'images', svs_name)
        svs_patch_paths = sorted(glob.glob(os.path.join(svs_patch_dir, "*.png")),
                key = lambda p: \
                [int(s) for s in re.split('\(|,|\)', p.split('/')[-1])[1:-1]])

        patch_coords = np.array([[int(s) 
            for s in re.split('\(|,|\)', p.split('/')[-1])[1:-1]] 
            for p in svs_patch_paths], dtype=int)

        # Get number of patches
        num_patches = patch_coords.shape[0]
        print(f'\n\t{svs_name}: generated {num_patches} patches')

        # Limit one-time predict to 5 GB data
        patch_masks = np.zeros(
                (num_patches, args.patch_size, args.patch_size, 3), 
                dtype=np.float32)
        num_patch_per_round = 5*1024**3 // \
                (np.prod(patch_masks.shape[1:]) * patch_masks.itemsize)
        num_rounds = int(np.ceil(num_patches / num_patch_per_round))
        print(f'\tBeginning {num_rounds} rounds of prediction')
        for ri in range(num_rounds):
            start_p = ri * num_patch_per_round
            end_p = min(num_patches, start_p + num_patch_per_round)

            # Create a data Sequence
            patches_dataset = BrainSegPredictSequence(
                    svs_patch_paths[start_p:end_p], args.batch_size)

            # Pass to model for prediction
            patch_masks[start_p:end_p, ...] \
                    = model.predict(patches_dataset,
                            verbose=1,
                            workers=os.cpu_count(),
                            use_multiprocessing=True)

        # Reconstruct whole image from patch_masks
        mask_arr = reconstruct_predicted_masks(patch_masks, patch_coords)
        del patch_masks, patch_coords

        # Save predicted masks
        save_predicted_masks(mask_arr, args.save_dir, svs_name)
        del mask_arr

        success_count += 1
        gc.collect()

    # Prompt for compute_mask_accuracy
    print('\nOut of %d WSIs, \n\t%d were successfully processed'
            % (len(svs_paths), success_count))
    print(f'To compute mask accuracy, please run compute_mask_accuracy.py {args.save_dir}')


if __name__ == '__main__':
    parser = get_parser()
    argcomplete.autocomplete(parser)
    predict_args = parser.parse_args()

    predict(predict_args)