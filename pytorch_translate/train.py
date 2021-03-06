#!/usr/bin/env python3

import argparse
import collections
import math
import multiprocessing.queues as mp_queues
import os
import random
import shutil
import time
from typing import Any, Callable, Dict, Optional, Tuple

import numpy as np
import torch
from fairseq import (
    criterions,
    data,
    distributed_utils,
    models,
    optim,
    options,
    progress_bar,
    tasks,
    utils,
)
from fairseq.meters import AverageMeter, StopwatchMeter
from fairseq.trainer import Trainer
from pytorch_translate import sequence_criterions  # noqa
from pytorch_translate import transformer  # noqa
from pytorch_translate import weighted_criterions  # noqa
from pytorch_translate import (
    average_checkpoints,
    constants,
    data as pytorch_translate_data,
    dictionary as pytorch_translate_dictionary,
    generate,
    multi_model,
    options as pytorch_translate_options,
    preprocess,
    utils as pytorch_translate_utils,
)
from pytorch_translate.research.knowledge_distillation import (  # noqa
    knowledge_distillation_loss,
)
from pytorch_translate.tasks.semi_supervised_task import PytorchTranslateSemiSupervised
from pytorch_translate.utils import ManagedCheckpoints
from pytorch_translate.word_prediction import word_prediction_criterion  # noqa
from pytorch_translate.word_prediction import word_prediction_model  # noqa


from pytorch_translate import rnn  # noqa; noqa

# we import semi_supervised here so that the model gets registered in fairseq
# model registry
from pytorch_translate import semi_supervised  # noqa; noqa


from pytorch_translate import char_source_model  # noqa; noqa


def get_parser_with_args(default_task="pytorch_translate"):
    parser = options.get_parser("Trainer", default_task=default_task)
    pytorch_translate_options.add_verbosity_args(parser, train=True)
    pytorch_translate_options.add_dataset_args(parser, train=True, gen=True)
    options.add_distributed_training_args(parser)
    # Adds args related to training (validation and stopping criterions).
    optimization_group = options.add_optimization_args(parser)
    pytorch_translate_options.expand_optimization_args(optimization_group)
    # Adds args related to checkpointing.
    checkpointing_group = options.add_checkpoint_args(parser)
    pytorch_translate_options.expand_checkpointing_args(checkpointing_group)
    # Add model related args
    options.add_model_args(parser)
    # Adds args for generating intermediate BLEU eval while training.
    generation_group = options.add_generation_args(parser)
    pytorch_translate_options.expand_generation_args(generation_group, train=True)
    # Adds args related to input data files (preprocessing, numberizing, and
    # binarizing text files; creating vocab files)
    pytorch_translate_options.add_preprocessing_args(parser)
    return parser


def default_extra_state(args) -> Dict[str, Any]:
    return {
        "epoch": 1,
        "batch_offset": 0,
        "start_time": time.time(),
        # We have both checkpoint_lowest_loss and tune_eval.lowest_loss since we
        # may have seen a lower loss during validation and updated
        # tune_eval.lowest_loss, but may not have written a new checkpoint with
        # that loss yet.
        "checkpoint_lowest_loss": None,
        "tune_eval": {
            "loss": None,
            "perplexity": None,
            "lowest_loss": None,
            "num_since_best": 0,
        },
        # "last_eval_bleu": 0,
        "tune_bleu": {
            "current": None,
            "best": None,
            "best_epoch": None,
            "num_since_best": 0,
            "last_eval_step": 0,
        },
        "last_checkpoints": ManagedCheckpoints(
            max(args.generate_bleu_eval_avg_checkpoints, args.max_checkpoints_kept),
            # Don't auto_clear checkpoints for no_epoch_checkpoints, because
            # we are only going to reuse the same file.
            auto_clear=(
                args.max_checkpoints_kept > 0 and not args.no_epoch_checkpoints
            ),
        ),
    }


def clear_per_step_extra_state(extra_state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Clear values in extra_state that are technically only true for a specific
    step (ex: the eval tune loss calculated after 5 train steps is no longer
    accurate after 7 train steps, but might not get updated since we might not
    be doing eval after every step).
    """
    extra_state["tune_eval"]["loss"] = None
    extra_state["tune_eval"]["perplexity"] = None
    extra_state["tune_bleu"]["current"] = None
    return extra_state


def load_existing_checkpoint(
    checkpoint_path, trainer, restore_state=True
) -> Tuple[bool, Optional[Dict]]:
    loaded = False
    extra_state = None

    if not os.path.isfile(checkpoint_path):
        print(
            f"| No existing checkpoint at {checkpoint_path}. "
            f"Starting training from scratch."
        )
        return loaded, extra_state

    if restore_state:
        extra_state = trainer.load_checkpoint(checkpoint_path)
        if extra_state is None:
            loaded = False
            print(f"| Failed to load checkpoint and state from {checkpoint_path}.")
        else:
            loaded = True
            print(
                f"| Loaded checkpoint {checkpoint_path} "
                f"(epoch {extra_state['epoch']}) with restored extra state."
            )
            # batch_offset being None denotes this was a checkpoint saved at
            # the end of an epoch (after the last batch).
            if extra_state["batch_offset"] is None:
                trainer.lr_step(extra_state["epoch"])
                extra_state["epoch"] += 1
                extra_state["batch_offset"] = 0

    else:
        dummy_state = trainer.load_checkpoint(checkpoint_path, reset_optimizer=True)
        if dummy_state is None:
            loaded = False
            print(f"| Failed to load checkpoint weights from {checkpoint_path}.")
        else:
            loaded = True
            print(f"| Loaded checkpoint weights from {checkpoint_path}.")

    return loaded, extra_state


def validate_and_set_default_args(args):
    # Prevents generate from printing individual translated sentences when
    # calculating BLEU score.
    args.quiet = True

    if args.local_num_gpus > args.distributed_world_size:
        raise ValueError(
            f"--local-num-gpus={args.local_num_gpus} must be "
            f"<= --distributed-world-size={args.distributed_world_size}."
        )
    if args.local_num_gpus > torch.cuda.device_count():
        raise ValueError(
            f"--local-num-gpus={args.local_num_gpus} must be "
            f"<= the number of GPUs: {torch.cuda.device_count()}."
        )
    # Set default init method for multi-GPU training if the user didn't specify
    # them.
    if args.distributed_world_size > 1:
        args.distributed_init_method = (
            f"tcp://localhost:{random.randint(10000, 20000)}"
            if not args.distributed_init_method
            else args.distributed_init_method
        )

    if not args.source_vocab_file:
        args.source_vocab_file = pytorch_translate_dictionary.default_dictionary_path(
            save_dir=args.save_dir, dialect=args.source_lang
        )
    if not args.target_vocab_file:
        args.target_vocab_file = pytorch_translate_dictionary.default_dictionary_path(
            save_dir=args.save_dir, dialect=args.target_lang
        )

    if args.arch == "char_source" and not args.char_source_vocab_file:
        args.char_source_vocab_file = pytorch_translate_dictionary.default_char_dictionary_path(
            save_dir=args.save_dir, dialect=args.source_lang
        )

    pytorch_translate_options.validate_preprocessing_args(args)
    pytorch_translate_options.validate_generation_args(args)
    if args.multiling_encoder_lang and not args.multiling_source_vocab_file:
        args.multiling_source_vocab_file = [
            pytorch_translate_dictionary.default_dictionary_path(
                save_dir=args.save_dir, dialect=f"src-{l}"
            )
            for l in args.multiling_encoder_lang
        ]
    if args.multiling_decoder_lang and not args.multiling_target_vocab_file:
        args.multiling_target_vocab_file = [
            pytorch_translate_dictionary.default_dictionary_path(
                save_dir=args.save_dir, dialect=f"trg-{l}"
            )
            for l in args.multiling_decoder_lang
        ]


def setup_training_model(args):
    """Parse args, load dataset, and build model with criterion."""
    if not torch.cuda.is_available():
        raise NotImplementedError("Training on CPU is not supported")
    torch.cuda.set_device(args.device_id)
    torch.manual_seed(args.seed)

    # Setup task and load dataset
    task = tasks.setup_task(args)

    # Build model and criterion
    model = task.build_model(args)
    print("| building criterion")
    criterion = task.build_criterion(args)
    print(f"| model {args.arch}, criterion {criterion.__class__.__name__}")
    print(
        f"| num. model params: \
        {sum(p.numel() for p in model.parameters())}"
    )

    if args.task == constants.SEMI_SUPERVISED_TASK:
        # TODO(T35638969): hide this inside the task itself, just use self.args
        task.load_dataset(
            split=args.train_subset,
            src_bin_path=args.train_source_binary_path,
            tgt_bin_path=args.train_target_binary_path,
            forward_model=task.forward_model,
            backward_model=task.backward_model,
        )
    elif args.task == "pytorch_translate_denoising_autoencoder":
        task.load_dataset(
            split=args.train_subset,
            src_bin_path=args.train_source_binary_path,
            tgt_bin_path=args.train_target_binary_path,
            seed=args.seed,
            use_noiser=True,
        )
    else:
        task.load_dataset(
            split=args.train_subset,
            src_bin_path=args.train_source_binary_path,
            tgt_bin_path=args.train_target_binary_path,
            weights_file=getattr(args, "train_weights_path", None),
        )
    task.load_dataset(
        split=args.valid_subset,
        src_bin_path=args.eval_source_binary_path,
        tgt_bin_path=args.eval_target_binary_path,
    )
    return task, model, criterion


def setup_training_state(args, trainer, task):
    """Set up the directory for saving checkpoints.
    Load pretrained model if specified."""
    os.makedirs(args.save_dir, exist_ok=True)

    # If --restore-file is already present under --save-dir, use that one
    # instead of --pretrained-checkpoint-file. The idea is that
    # --pretrained-checkpoint-file allows the user to specify restoring from a
    # different run's checkpoint (possibly with different training params),
    # while not polluting the previous run's checkpoint directory
    # with new checkpoints. However, if training gets interrupted
    # and the user restarts training, we want to resume from
    # the checkpoints under --save-dir, instead of
    # restarting again from the old run's checkpoint at
    # --pretrained-checkpoint-file.
    #
    # Note that if args.restore_file is an absolute path, os.path.join() will
    # ignore previous directory args and just use the absolute path as is.
    checkpoint_path = os.path.join(args.save_dir, args.restore_file)
    restore_state = True
    if os.path.isfile(checkpoint_path):
        print(
            f"| Using --save-dir={args.save_dir}, --restore-file={args.restore_file}."
        )
    elif args.pretrained_checkpoint_file and os.path.isfile(
        args.pretrained_checkpoint_file
    ):
        checkpoint_path = args.pretrained_checkpoint_file
        restore_state = args.load_pretrained_checkpoint_state
        print(
            f"| Using --pretrained-checkpoint-file={args.pretrained_checkpoint_file}, "
            f"--load-pretrained-checkpoint-state={args.load_pretrained_checkpoint_state}."
        )

    extra_state = default_extra_state(args)
    if not os.path.isfile(checkpoint_path) and args.multi_model_restore_files:
        print(f"| Restoring individual models from {args.multi_model_restore_files}")
        multi_model.import_individual_models(args.multi_model_restore_files, trainer)
    else:
        loaded, loaded_extra_state = load_existing_checkpoint(
            checkpoint_path=checkpoint_path,
            trainer=trainer,
            restore_state=restore_state,
        )
        if loaded_extra_state:
            extra_state.update(loaded_extra_state)
        if loaded:
            args.path = checkpoint_path
            calculate_bleu_on_subset(
                args=args,
                task=task,
                epoch_str="initial loaded checkpoint",
                offset=None,
                dataset_split=args.valid_subset,
            )
    print(f"| extra_state: {extra_state}")
    return extra_state


def build_trainer(args, task, model, criterion, trainer_class):
    """ Build trainer with provided trainer_class, and set up training state.
    """
    # Make a dummy batch to (i) warm the caching allocator and (ii) as a
    # placeholder DistributedDataParallel when there's an uneven number of
    # batches per worker.
    max_positions = utils.resolve_max_positions(
        task.max_positions(), model.max_positions()
    )
    dummy_batch = task.dataset("train").get_dummy_batch(args.max_tokens, max_positions)

    # Build trainer
    trainer = trainer_class(args, task, model, criterion, dummy_batch)

    print(
        f"| training on {args.distributed_world_size} total GPUs "
        f"({torch.cuda.device_count()} GPUs locally on this machine)."
    )
    print(
        f"| max tokens per GPU = {args.max_tokens} and \
        max sentences per GPU = {args.max_sentences}",
        flush=True,
    )
    extra_state = setup_training_state(args, trainer, task)

    epoch_itr = task.get_batch_iterator(
        dataset=task.dataset(args.train_subset),
        max_tokens=args.max_tokens,
        max_sentences=args.max_sentences,
        max_positions=max_positions,
        ignore_invalid_inputs=args.skip_invalid_size_inputs_valid_test,
        required_batch_size_multiple=8,
        seed=args.seed,
        num_shards=args.distributed_world_size,
        shard_id=args.distributed_rank,
    )
    epoch = extra_state["epoch"]
    if extra_state["batch_offset"] == 0:
        epoch -= 1  # this will be incremented when we call epoch_itr.next_epoch_itr()
    epoch_itr.load_state_dict(
        {"epoch": epoch, "iterations_in_epoch": extra_state["batch_offset"]}
    )
    return trainer, extra_state, epoch_itr


def setup_training(args, trainer_class=None):
    """ Perform several steps:
    - build model using provided criterion and task
    - load data
    - build trainer, and set up training state
    """
    task, model, criterion = setup_training_model(args)

    if trainer_class is None:
        trainer_class = Trainer

    trainer, extra_state, epoch_itr = build_trainer(
        args=args,
        task=task,
        model=model,
        criterion=criterion,
        trainer_class=trainer_class,
    )

    return extra_state, trainer, task, epoch_itr


def create_prune_masks(args, trainer):
    """Generates binary masks for setting model weights to zero"""
    assert (
        args.pruning_percentile > 0 and args.pruning_percentile < 100
    ), "--pruning-percentile must be in (0, 100)"
    all_params = []
    if args.parameters_to_prune == "all":
        parameter_name = "weight"
    elif args.parameters_to_prune == "embed":
        parameter_name = "embed_tokens"
    elif args.parameters_to_prune == "lstm":
        parameter_name = "weight_"
    for name, params in trainer.model.named_parameters():
        if parameter_name in name:
            all_params.append(np.abs(np.reshape(params.data, (-1, 1))))
    threshold = np.percentile(np.vstack(all_params), args.pruning_percentile)

    prune_masks = {}
    for name, params in trainer.model.named_parameters():
        if parameter_name in name:
            prune_masks[name] = np.abs(params.data) < threshold

    return prune_masks


def apply_prune_masks(prune_masks, trainer):
    """Selectively sets model weights to zero using a binary mask."""

    for name, params in trainer.model.named_parameters():
        if name in prune_masks:
            params.data[prune_masks[name]] = 0.0


def train(
    args,
    extra_state: Dict[str, Any],
    trainer,
    task,
    epoch_itr,
    output_queue: Optional[mp_queues.Queue] = None,
    **train_step_kwargs,
):
    # offset for current epoch (may be different from checkpoint offset)
    starting_offset = extra_state["batch_offset"]

    # Train until the learning rate gets too small
    max_epoch = args.max_epoch or math.inf
    lr = trainer.get_lr()
    train_meter = StopwatchMeter()
    train_meter.start()
    stop_training_mid_epoch = False
    stop_training_end_of_epoch = False

    do_prune = args.pruning_percentile > 0
    if do_prune:
        prune_masks = create_prune_masks(args, trainer)
        apply_prune_masks(prune_masks, trainer)

    while lr > args.min_lr and extra_state["epoch"] <= max_epoch:
        """Train the model for one epoch."""

        itr, progress, extra_meters = setup_epoch(
            args=args, epoch_itr=epoch_itr, trainer=trainer
        )

        for i, samples in enumerate(progress, start=starting_offset):
            clear_per_step_extra_state(extra_state)
            extra_state["num_iterations"] = extra_state.get("num_iterations", 0) + 1
            if (
                train_step_kwargs is not None
                and "augment_adv" in train_step_kwargs.keys()
            ):
                train_step_kwargs["augment_adv"] = (
                    extra_state["epoch"] > args.warmup_epochs
                )
            try:
                log_output = trainer.train_step(samples, **train_step_kwargs)
            # Fairseq's fp16_trainer raises this uncommon error to indicate
            # that we should stop training.
            except FloatingPointError as e:
                print(f"Stopping training due to: {e}.")
                stop_training_mid_epoch = True
                break

            if do_prune:
                apply_prune_masks(prune_masks, trainer)

            if i == starting_offset:
                # ignore the first mini-batch in words-per-second calculation
                trainer.get_meter("wps").reset()

            num_iterations = extra_state["num_iterations"]
            do_eval_tune_loss = (
                args.subepoch_validate_interval > 0
                and num_iterations % args.subepoch_validate_interval == 0
            )
            do_save = (
                not args.no_save
                and args.save_interval_updates > 0
                and num_iterations % args.save_interval_updates == 0
            )
            do_eval_bleu = (
                # We can only do BLEU eval when we have a new checkpoint to load.
                do_save
                and args.generate_bleu_eval_interval > 0
                and num_iterations - extra_state["tune_bleu"]["last_eval_step"]
                >= args.generate_bleu_eval_interval
            )
            if do_eval_bleu:
                extra_state["tune_bleu"]["last_eval_step"] = num_iterations

            extra_state["batch_offset"] = i + 1
            (extra_state, stop_training_mid_epoch, translation_samples) = save_and_eval(
                args=args,
                trainer=trainer,
                task=task,
                extra_state=extra_state,
                do_eval_tune_loss=do_eval_tune_loss,
                do_save=do_save,
                do_eval_bleu=do_eval_bleu,
            )

            # This should come after save_and_eval. Even if log_output is None,
            # meaning that there was an overflow,  We should still run
            # save_and_eval to sync all_reduce and then skip the batch.
            if log_output is None:
                # This indicates that the batch was skipped, typically
                # because of OOM or FP16 overflow.
                continue

            train_stats = log_mid_epoch_stats(
                trainer=trainer,
                progress=progress,
                extra_meters=extra_meters,
                log_output=log_output,
            )

            if distributed_utils.is_master(args) and output_queue is not None:
                output_queue.put_nowait(
                    (
                        trainer.get_num_updates(),
                        {
                            "train_ppl": train_stats["ppl"],
                            "tune_ppl": extra_state["tune_eval"]["perplexity"],
                            "tune_bleu": extra_state["tune_bleu"]["current"],
                            "translation_samples": translation_samples,
                        },
                    )
                )

            if (
                do_eval_bleu
                and args.shrink_lr_no_best_bleu_eval > 0
                and extra_state["tune_bleu"]["num_since_best"]
                > args.shrink_lr_no_best_bleu_eval
            ):
                current_lr = trainer.optimizer.get_lr()
                trainer.optimizer.set_lr(current_lr * args.lr_shrink)
                lr = trainer.optimizer.get_lr()
                print(f"Decayed lr from {current_lr} to {lr}.")

            stop_training_mid_epoch = (
                stop_training_mid_epoch
                or is_training_over_time_limit(
                    extra_state["start_time"], args.stop_time_hr
                )
            )
            if stop_training_mid_epoch:
                break

        # log end-of-epoch stats
        train_stats = log_end_epoch_stats(
            trainer=trainer, progress=progress, extra_meters=extra_meters
        )

        # Run a training step if not stopping mid-epoch.
        if not stop_training_mid_epoch:
            # batch_offset being None denotes the end of an epoch.
            extra_state["batch_offset"] = None
            (
                extra_state,
                stop_training_end_of_epoch,
                translation_samples,
            ) = save_and_eval(
                args=args,
                trainer=trainer,
                task=task,
                extra_state=extra_state,
                do_eval_tune_loss=True,
                do_save=not args.no_save and not args.no_end_of_epoch_checkpoints,
                do_eval_bleu=args.generate_bleu_eval_per_epoch,
            )
            if distributed_utils.is_master(args) and output_queue is not None:
                output_queue.put_nowait(
                    (
                        trainer.get_num_updates(),
                        {
                            "train_ppl": train_stats["ppl"],
                            "tune_ppl": extra_state["tune_eval"]["perplexity"],
                            "tune_bleu": extra_state["tune_bleu"]["current"],
                            "translation_samples": translation_samples,
                        },
                    )
                )

        if stop_training_mid_epoch or stop_training_end_of_epoch:
            break

        lr = trainer.lr_step(extra_state["epoch"], extra_state["tune_eval"]["loss"])
        extra_state["epoch"] += 1
        extra_state["batch_offset"] = 0
        starting_offset = 0

    train_meter.stop()
    print(f"| done training in {train_meter.sum:.1f} seconds")
    print(
        f"| Best BLEU score of {extra_state['tune_bleu']['best']} was from "
        f"epoch {extra_state['tune_bleu']['best_epoch']}"
    )


def is_training_over_time_limit(start_time: float, stop_time: float):
    elapsed_hr = (time.time() - start_time) / (60 * 60)
    training_over_time_limit = False
    if stop_time >= 0 and elapsed_hr > stop_time:
        print(
            f"Stopping training due to stop time limit - it has been  "
            f"{elapsed_hr} hours since starting training at {start_time}."
        )
        training_over_time_limit = True
    return training_over_time_limit


def get_perplexity(loss):
    try:
        return f"{math.pow(2, loss):.2f}"
    except OverflowError:
        return float("inf")


def setup_epoch(args, epoch_itr, trainer):
    """Sets up data and progress meters for one epoch."""
    # Update parameters every N batches
    if epoch_itr.epoch <= len(args.update_freq):
        update_freq = args.update_freq[epoch_itr.epoch - 1]
    else:
        update_freq = args.update_freq[-1]

    # Initialize dataloader, starting at batch_offset
    itr = epoch_itr.next_epoch_itr()
    itr = data.iterators.GroupedIterator(itr, update_freq)
    progress = progress_bar.build_progress_bar(
        args, itr, epoch_itr.epoch, no_progress_bar="simple"
    )

    # reset training meters
    for k in [
        "train_loss",
        "train_nll_loss",
        "wps",
        "ups",
        "wpb",
        "bsz",
        "gnorm",
        "clip",
    ]:
        meter = trainer.get_meter(k)
        if meter is not None:
            meter.reset()

    extra_meters = collections.defaultdict(lambda: AverageMeter())
    return itr, progress, extra_meters


def log_mid_epoch_stats(trainer, progress, extra_meters, log_output):
    stats = get_training_stats(trainer)
    for k, v in log_output.items():
        if k in ["loss", "nll_loss", "ntokens", "nsentences", "sample_size"]:
            continue  # these are already logged above
        if "loss" in k:
            extra_meters[k].update(v, log_output["sample_size"])
        else:
            extra_meters[k].update(v)
        stats[k] = extra_meters[k].avg
    progress.log(stats)
    return stats


def log_end_epoch_stats(trainer, progress, extra_meters):
    stats = get_training_stats(trainer)
    for k, meter in extra_meters.items():
        stats[k] = meter.avg
    progress.print(stats)
    return stats


def get_training_stats(trainer):
    stats = collections.OrderedDict()
    stats["loss"] = f"{trainer.get_meter('train_loss').avg:.3f}"
    if trainer.get_meter("train_nll_loss").count > 0:
        nll_loss = trainer.get_meter("train_nll_loss").avg
        stats["nll_loss"] = f"{nll_loss:.3f}"
    else:
        nll_loss = trainer.get_meter("train_loss").avg
    stats["ppl"] = get_perplexity(nll_loss)
    stats["wps"] = round(trainer.get_meter("wps").avg)
    stats["ups"] = f"{trainer.get_meter('ups').avg:.1f}"
    stats["wpb"] = round(trainer.get_meter("wpb").avg)
    stats["bsz"] = round(trainer.get_meter("bsz").avg)
    stats["num_updates"] = trainer.get_num_updates()
    stats["lr"] = trainer.get_lr()
    stats["gnorm"] = f"{trainer.get_meter('gnorm').avg:.3f}"
    stats["clip"] = f"{trainer.get_meter('clip').avg:.0%}"
    stats["oom"] = trainer.get_meter("oom").avg
    if trainer.get_meter("loss_scale") is not None:
        stats["loss_scale"] = f"{trainer.get_meter('loss_scale').avg:.3f}"
    stats["wall"] = round(trainer.get_meter("wall").elapsed_time)
    stats["train_wall"] = round(trainer.get_meter("train_wall").sum)
    return stats


def save_checkpoint(trainer, args, extra_state):
    epoch = extra_state["epoch"]
    batch_offset = extra_state["batch_offset"]
    tune_loss = extra_state["tune_eval"]["loss"]

    if args.log_verbose:
        print(
            f"| Preparing to save checkpoints for epoch {epoch}, "
            f"offset {batch_offset}. ",
            flush=True,
        )

    # batch_offset being None means that we're at the end of an epoch.
    if batch_offset is None:
        if not args.no_epoch_checkpoints:
            epoch_filename = os.path.join(args.save_dir, f"checkpoint{epoch}.pt")
            trainer.save_checkpoint(epoch_filename, extra_state)
            extra_state["last_checkpoints"].append(epoch_filename)

        assert tune_loss is not None

        if (
            extra_state["checkpoint_lowest_loss"] is None
            or tune_loss < extra_state["checkpoint_lowest_loss"]
        ):
            extra_state["checkpoint_lowest_loss"] = tune_loss
            best_filename = os.path.join(args.save_dir, "checkpoint_best.pt")
            trainer.save_checkpoint(best_filename, extra_state)

    # Otherwise, we're in the middle of an epoch.
    elif not args.no_epoch_checkpoints:
        epoch_filename = os.path.join(
            args.save_dir, f"checkpoint{epoch}_{batch_offset}.pt"
        )
        trainer.save_checkpoint(epoch_filename, extra_state)
        extra_state["last_checkpoints"].append(epoch_filename)

    last_filename = os.path.join(args.save_dir, constants.LAST_CHECKPOINT_FILENAME)
    trainer.save_checkpoint(last_filename, extra_state)

    # This ensures we'll always have at least one checkpoint in the list to use
    # for BLEU eval, even if we're not saving epoch checkpoints.
    if args.no_epoch_checkpoints:
        extra_state["last_checkpoints"].append(epoch_filename)
    if args.log_verbose:
        print(
            f"| Finished saving checkpoints for epoch {epoch}, "
            f"offset {batch_offset}.",
            flush=True,
        )
    return extra_state


def eval_tune_loss(args, trainer, task, subset, extra_state):
    """Evaluate the model on the validation set and return the average loss."""
    # Initialize dataloader
    itr = task.get_batch_iterator(
        dataset=task.dataset(subset),
        max_tokens=args.max_tokens,
        max_sentences=args.max_sentences_valid,
        max_positions=utils.resolve_max_positions(
            task.max_positions(), trainer.get_model().max_positions()
        ),
        ignore_invalid_inputs=args.skip_invalid_size_inputs_valid_test,
        required_batch_size_multiple=8,
        seed=args.seed,
        num_shards=args.distributed_world_size,
        shard_id=args.distributed_rank,
    ).next_epoch_itr(shuffle=False)
    progress = progress_bar.build_progress_bar(
        args=args,
        iterator=itr,
        epoch=extra_state["epoch"],
        prefix=f"valid on '{subset}' subset",
        no_progress_bar="simple",
    )

    # reset validation loss meters
    for k in ["valid_loss", "valid_nll_loss"]:
        meter = trainer.get_meter(k)
        if meter is not None:
            meter.reset()

    extra_meters = collections.defaultdict(lambda: AverageMeter())
    for sample in progress:
        log_output = trainer.valid_step(sample)

        # log mid-validation stats
        stats = get_valid_stats(trainer)
        for k, v in log_output.items():
            if k in ["loss", "nll_loss", "ntokens", "nsentences", "sample_size"]:
                continue
            if "loss" in k:
                extra_meters[k].update(v, log_output["sample_size"])
            else:
                extra_meters[k].update(v)
            stats[k] = extra_meters[k].avg
        progress.log(stats)

    # log validation stats
    stats = get_valid_stats(trainer)
    for k, meter in extra_meters.items():
        stats[k] = meter.avg
    progress.print(stats)

    extra_state["tune_eval"]["loss"] = stats["valid_loss"]
    extra_state["tune_eval"]["perplexity"] = stats["valid_ppl"]

    if (
        extra_state["tune_eval"]["lowest_loss"] is None
        or extra_state["tune_eval"]["loss"] < extra_state["tune_eval"]["lowest_loss"]
    ):
        extra_state["tune_eval"]["lowest_loss"] = extra_state["tune_eval"]["loss"]
        extra_state["tune_eval"]["num_since_best"] = 0
    else:
        extra_state["tune_eval"]["num_since_best"] += 1

    stop_due_to_tune_loss = False
    if (
        args.stop_no_best_validate_loss >= 0
        and extra_state["tune_eval"]["num_since_best"] > args.stop_no_best_validate_loss
    ):
        stop_due_to_tune_loss = True
        print(
            f"Stopping training due to eval tune loss stagnation - last best "
            f"eval tune loss of {extra_state['tune_eval']['lowest_loss']} "
            f"(current loss: {extra_state['tune_eval']['loss']}) "
            f"was {extra_state['tune_eval']['num_since_best']} validations ago."
        )
    return extra_state, stop_due_to_tune_loss


def get_valid_stats(trainer):
    stats = collections.OrderedDict()
    stats["valid_loss"] = trainer.get_meter("valid_loss").avg
    if trainer.get_meter("valid_nll_loss").count > 0:
        nll_loss = trainer.get_meter("valid_nll_loss").avg
        stats["valid_nll_loss"] = nll_loss
    else:
        nll_loss = trainer.get_meter("valid_loss").avg
    stats["valid_ppl"] = get_perplexity(nll_loss)
    return stats


def save_averaged_checkpoint(args, extra_state):
    epoch, offset = extra_state["epoch"], extra_state["batch_offset"]
    if not hasattr(save_averaged_checkpoint, "last_avg_checkpoints"):
        if args.max_checkpoints_kept == 0:
            raise argparse.ArgumentTypeError("--max-checkpoints-kept must be != 0.")
        save_averaged_checkpoint.last_avg_checkpoints = ManagedCheckpoints(
            max(args.max_checkpoints_kept, 1), auto_clear=args.max_checkpoints_kept > 0
        )

    last_checkpoints = extra_state["last_checkpoints"].get_last_n(
        1 if args.no_epoch_checkpoints else args.generate_bleu_eval_avg_checkpoints
    )
    if args.log_verbose:
        print(
            f"| Reading {len(last_checkpoints)} previous "
            f"checkpoints for averaging in epoch {epoch}, offset {offset}.",
            flush=True,
        )
    averaged_state = average_checkpoints.average_checkpoints(last_checkpoints)
    filename = os.path.join(args.save_dir, f"averaged_checkpoint{epoch}_{offset}.pt")
    save_averaged_checkpoint.last_avg_checkpoints.append(filename)
    if args.log_verbose:
        print(
            f"| Preparing to save averaged checkpoint for "
            f"epoch {epoch}, offset {offset}.",
            flush=True,
        )
    utils.torch_persistent_save(averaged_state, filename)
    if args.log_verbose:
        print(
            f"| Finished saving averaged checkpoint for "
            f"epoch {epoch}, offset {offset}.",
            flush=True,
        )
    return filename


def calculate_bleu_on_subset(args, task, epoch_str: str, offset, dataset_split):
    # This is a trick to have generate use max_sentences_valid
    max_sentences_train = args.max_sentences
    args.max_sentences = args.max_sentences_valid
    datasets = []
    lang_pairs = []
    """
    In multi model training set up, evaluate one model at a time with
    corresponding dataset
    lang_pair is passed to identify model to be used for generation
    """
    if isinstance(task, PytorchTranslateSemiSupervised):
        for key, dataset in task.datasets[dataset_split].datasets.items():
            datasets.append(dataset)
            lang_pairs.append(key)
    else:
        datasets = [task.dataset(dataset_split)]
        lang_pairs = [None]
    score_aggregator_fn = (
        task.score_aggregator if hasattr(task, "score_aggregator") else sum
    )
    scores = []
    ensemble_models, _ = utils.load_ensemble_for_inference(args.path.split(":"), task)
    for dataset, lang_pair in zip(datasets, lang_pairs):
        # Generate score
        scorer, num_sentences, gen_timer, translation_samples = generate.generate_score(
            args=args,
            task=task,
            dataset=dataset,
            models=ensemble_models,
            lang_pair=lang_pair,
        )
        scores.append(scorer.score())
        print(
            f"| epoch {epoch_str} | offset {offset} "
            f"| Eval on {dataset_split} {lang_pair if lang_pair else ''} subset "
            f"with beam={args.beam}: {scorer.result_string()}. "
            f"Generated {num_sentences} sentences ({gen_timer.n} tokens) "
            f"in {gen_timer.sum:.1f}s ({1. / gen_timer.avg:.2f} tokens/s).",
            flush=True,
        )
    # Set max_sentences to its original value
    args.max_sentences = max_sentences_train
    return score_aggregator_fn(scores), translation_samples


def evaluate_bleu(args, task, extra_state):
    epoch, offset = extra_state["epoch"], extra_state["batch_offset"]
    filename = save_averaged_checkpoint(args, extra_state)
    args.path = filename
    extra_state["tune_bleu"]["current"], translation_samples = calculate_bleu_on_subset(
        args=args,
        task=task,
        epoch_str=f"{epoch:03d}",
        offset=offset,
        dataset_split=args.valid_subset,
    )

    if (
        extra_state["tune_bleu"]["best"] is None
        or extra_state["tune_bleu"]["current"] > extra_state["tune_bleu"]["best"]
    ):
        extra_state["tune_bleu"]["best"] = extra_state["tune_bleu"]["current"]
        extra_state["tune_bleu"]["best_epoch"] = epoch
        extra_state["tune_bleu"]["num_since_best"] = 0
        best_filename = os.path.join(
            args.save_dir, constants.AVERAGED_CHECKPOINT_BEST_FILENAME
        )
        shutil.copy2(filename, best_filename)
    else:
        extra_state["tune_bleu"]["num_since_best"] += 1

    stop_due_to_tune_bleu = False
    if (
        args.stop_no_best_bleu_eval >= 0
        and extra_state["tune_bleu"]["num_since_best"] > args.stop_no_best_bleu_eval
    ):
        stop_due_to_tune_bleu = True
        print(
            f"Stopping training due to BLEU score stagnation on tune set - "
            f"last best BLEU score of {extra_state['tune_bleu']['best']} "
            f"(current score: {extra_state['tune_bleu']['current']}) was "
            f"{extra_state['tune_bleu']['num_since_best']} evals ago."
        )
    return extra_state, stop_due_to_tune_bleu, translation_samples


def save_and_eval(
    args,
    trainer,
    task,
    extra_state: Dict[str, Any],
    do_eval_tune_loss: bool,
    do_save: bool,
    do_eval_bleu: bool,
) -> Tuple[Dict[str, Any], bool, Optional[list]]:
    # Clear any remaining metrics from previous steps. This should already
    # have been done before, but just in case - to make sure we catch
    # any case where extra_case does not get populated correctly.
    extra_state = clear_per_step_extra_state(extra_state)

    # Under multiprocessing, each process will run eval over a different
    # shard of the tune data set and then aggregate the results across all
    # processes, so the eval stats from all processes' trainer should
    # remain synchronized.
    stop_due_to_tune_loss = False
    if do_eval_tune_loss:
        extra_state, stop_due_to_tune_loss = eval_tune_loss(
            args=args,
            trainer=trainer,
            task=task,
            subset=args.valid_subset,
            extra_state=extra_state,
        )

    # Only save checkpoints and eval tune BLEU on the master - all other
    # processes will just get the results from the master.
    master_extra_state = None
    master_stop_training = None
    translation_samples = None
    if distributed_utils.is_master(args):
        stop_due_to_tune_bleu = False
        if do_save:
            extra_state = save_checkpoint(
                trainer=trainer, args=args, extra_state=extra_state
            )
        if do_eval_bleu and not do_save:
            raise ValueError(
                "do_save should always be true when do_eval_bleu is true "
                "since a new BLEU eval can only be done when there's a new "
                "checkpoint."
            )
        if do_eval_bleu:
            extra_state, stop_due_to_tune_bleu, translation_samples = evaluate_bleu(
                args=args, task=task, extra_state=extra_state
            )
        master_extra_state = extra_state
        master_stop_training = stop_due_to_tune_loss or stop_due_to_tune_bleu

    # We don't all_gather the translation_samples since the sample sentences
    # could be pretty long, and only the master uses it anyway.
    extra_state, stop_training = pytorch_translate_utils.all_gather_from_master(
        args=args, data=[master_extra_state, master_stop_training]
    )

    # Basic sanity checks that extra_state is populated correctly.
    assert not (
        do_eval_tune_loss
        and (
            extra_state["tune_eval"]["loss"] is None
            or extra_state["tune_eval"]["perplexity"] is None
        )
    )
    assert not (do_eval_bleu and extra_state["tune_bleu"]["current"] is None)
    return extra_state, stop_training, translation_samples


def single_process_main(args, trainer_class=Trainer, **train_step_kwargs):
    """Train the model for multiple epochs."""
    pytorch_translate_options.print_args(args)
    extra_state, trainer, task, epoch_itr = setup_training(args, trainer_class)
    train(
        args=args,
        extra_state=extra_state,
        trainer=trainer,
        task=task,
        epoch_itr=epoch_itr,
        **train_step_kwargs,
    )


def multi_process_train(
    device_id: int,
    args,
    output_queue: Optional[mp_queues.Queue],
    start_rank: int = 0,
    init_fn: Optional[Callable[[], None]] = None,
    trainer_class=None,
    train_step_kwargs=None,
):
    if init_fn:
        init_fn()
    args.device_id = device_id
    args.distributed_rank = start_rank + device_id
    torch.cuda.set_device(args.device_id)
    if args.distributed_world_size > 1:
        args.distributed_rank = distributed_utils.distributed_init(args)
    extra_state, trainer, task, epoch_itr = setup_training(args, trainer_class)
    train(
        args=args,
        extra_state=extra_state,
        trainer=trainer,
        task=task,
        epoch_itr=epoch_itr,
        output_queue=output_queue,
        **train_step_kwargs,
    )


def multi_process_main(
    args: Any,
    use_output_queue: bool,
    start_rank: int = 0,
    init_fn: Optional[Callable[[], None]] = None,
    trainer_class=None,
    **train_step_kwargs,
):
    pytorch_translate_options.print_args(args)
    output_queue = (
        torch.multiprocessing.get_context("spawn").Queue() if use_output_queue else None
    )
    # Train with multiprocessing.
    spawn_context = torch.multiprocessing.spawn(
        fn=multi_process_train,
        args=(
            args,
            output_queue,
            start_rank,
            init_fn,
            trainer_class,
            train_step_kwargs,
        ),
        nprocs=args.local_num_gpus,
        # We don't block here to allow caller to process output_queue in
        # parallel with training.
        join=False,
    )
    return (spawn_context, output_queue)


def main(args, trainer_class=Trainer, **train_step_kwargs):
    # We preprocess the data (generating vocab files and binarized data files
    # if needed) outside of the train processes to prevent them from having to
    # wait while the master process is doing this.
    preprocess.preprocess_corpora(args)

    if args.distributed_world_size == 1:
        single_process_main(args, trainer_class, **train_step_kwargs)
    else:
        spawn_context, _ = multi_process_main(
            args=args, use_output_queue=False, start_rank=0
        )
        while not spawn_context.join():
            pass


if __name__ == "__main__":
    parser = get_parser_with_args()
    args = options.parse_args_and_arch(parser)
    validate_and_set_default_args(args)
    main(args)
