import json
import os
from typing import Dict, List, Optional, Union

import peft
import safetensors  # type: ignore
import torch
import tqdm  # type: ignore
from peft.peft_model import PeftModel
from peft.tuners import lora
from transformers import PreTrainedModel  # type: ignore

from .xlora_classifier import xLoRAClassifier
from .xlora_config import xLoRAConfig
from .xlora_insertion import BaseTunerWrapper, PeftModelWrapper, xLoRALayer


def convert_layers_to_xlora(
    base: PeftModel,
    verbose: bool,
    top_k_lora: Optional[int] = None,
) -> int:
    """
    Returns the number of swapped layers.
    """
    assert isinstance(base.base_model, lora.LoraModel)
    total_swapped = 0

    scaling_keys = None
    for module in base.modules():
        if isinstance(module, lora.LoraLayer):
            if not scaling_keys:
                scaling_keys = list(module.scaling.keys())  # NOTE(EricLBuehler): Python 3.7: dicts are ordered!
            new_layer = xLoRALayer(
                model=base,
                target=module,
                target_forward=module.forward,
                scaling_keys=scaling_keys,
                top_k_lora=top_k_lora,
                layer_number=total_swapped,
            )
            module.forward = new_layer.forward
            total_swapped += 1
    if verbose:
        print(
            f"LoRA -> xLoRA complete: Swapped {total_swapped} LoRA layers (out of {len(list(base.modules()))} modules)."
        )

    return total_swapped


def add_xlora_to_model(
    model: PreTrainedModel,
    xlora_config: xLoRAConfig,
    adapters: Dict[str, str],
    verbose: bool,
) -> PeftModel:
    """
    This method converts all LoRA adapters to xLoRA layers, and it is one of the intended entrypoints
    for use of xLoRA. All LoRA adapters will be frozen, and the xLoRAClassifier is initialized.

    Args:
        model (`PreTrainedModel`):
            The model to add the LoRA adapters to. It may be modified in place.
        verbose (`bool`):
            Display tqdm, total swapping count.
        adapters (`dict`):
            Mapping of adapter names to the LoRA adapter id, as per PeftModel.load_adapter. *They will be automatically loaded*, to use as LoRA experts.
    Returns:
        model (`PeftModel`):
            The new model.
    """

    use_trainable_adapters = xlora_config.use_trainable_adapters
    adapters_items = iter(tqdm.tqdm(adapters.items()))
    first_item = next(adapters_items)
    model_peft = PeftModel.from_pretrained(model, first_item[1], first_item[0], is_trainable=use_trainable_adapters)

    for adapter_name, model_id in adapters_items:
        model_peft.load_adapter(model_id, adapter_name, is_trainable=use_trainable_adapters)

    model_peft.base_model.set_adapter(list(adapters.keys()))

    def hook(module, *args, **kwargs) -> None:
        args_real = args[0]
        kwargs_real: dict = args[1]
        kwargs_real.update(kwargs)

        xlora_classifier: xLoRAClassifier = model_peft.internal_xlora_classifier

        if "_xlora_classifier_inhibitor_flag" in kwargs_real:
            batch_size = kwargs_real["_xlora_classifier_inhibitor_flag"]

            del kwargs_real["_xlora_classifier_inhibitor_flag"]

            model_peft.internal_xlora_scalings = (
                torch.ones(batch_size, xlora_classifier.n_layers, xlora_classifier.n_classes, requires_grad=True)
                / xlora_classifier.n_classes
            )  # TODO(EricLBuehler): is the requires_grad=True necessary?

            return

        xlora_scalings = xlora_classifier.forward(
            *args_real,
            **kwargs_real,
        )
        model_peft.internal_xlora_scalings = xlora_scalings  # Set the scalings

    model.register_forward_pre_hook(hook, with_kwargs=True, prepend=True)

    if not use_trainable_adapters:
        model_peft.base_model.eval()
        for name, param in model_peft.base_model.named_parameters():
            if "lora_" in name:
                param.requires_grad = False

    assert isinstance(model_peft.base_model, peft.tuners.lora.LoraModel)

    total_swapped = convert_layers_to_xlora(
        model_peft,
        verbose,
        xlora_config.top_k_lora,
    )

    n_classes = len(adapters)
    xlora_classifier = xLoRAClassifier(model_peft, xlora_config, n_classes, total_swapped)

    # Setup the internal state
    base_model_wrapper = BaseTunerWrapper(model_peft.base_model, xlora_classifier)
    model_peft.base_model.forward = base_model_wrapper.forward  # type: ignore[method-assign]

    peft_model_wrapper = PeftModelWrapper(
        model_peft, model_peft.save_pretrained, xlora_config, model_peft.get_nb_trainable_parameters
    )
    model_peft.save_pretrained = peft_model_wrapper.save_pretrained  # type: ignore[method-assign]

    assert not hasattr(model_peft, "set_use_trainable_adapters")
    model_peft.set_use_trainable_adapters = peft_model_wrapper.set_use_trainable_adapters  # type: ignore

    assert not hasattr(model_peft, "print_scalings_predictions")
    model_peft.print_scalings_predictions = peft_model_wrapper.print_scalings_predictions  # type: ignore

    assert not hasattr(model_peft, "enable_scalings_logging")
    model_peft.enable_scalings_logging = peft_model_wrapper.enable_scalings_logging  # type: ignore

    assert not hasattr(model_peft, "disable_scalings_logging")
    model_peft.disable_scalings_logging = peft_model_wrapper.disable_scalings_logging  # type: ignore

    assert not hasattr(model_peft, "flush_log_scalings")
    model_peft.flush_log_scalings = peft_model_wrapper.flush_log_scalings  # type: ignore

    model_peft.get_nb_trainable_parameters = peft_model_wrapper.get_nb_trainable_parameters  # type: ignore

    model_peft.print_trainable_parameters = peft_model_wrapper.print_trainable_parameters  # type: ignore

    # Setup the model internal state
    assert not hasattr(model_peft, "internal_xlora_classifier")
    model_peft.internal_xlora_classifier = xlora_classifier

    assert not hasattr(model_peft, "internal_xlora_scalings")
    model_peft.internal_xlora_scalings = None  # type: ignore

    return model_peft


def from_pretrained(
    load_directory: str,
    model: PreTrainedModel,
    adapters: Union[List[str], Dict[str, str]],
    verbose: bool,
    device: str,
    from_safetensors: bool = True,
) -> PeftModel:
    """
    Loads a pretrained classifier and potentially adapters from the specified folder while initializing the model. This is the counterpart to `save_pretrained`.
    If trainable adapters was enabled, those saved adapters will be loaded.

    This method is very similar to `add_xlora_to_model`: it converts all LoRA adapters to xLoRA layers, and it is one of
    the intended entrypoints for use of xLoRA. All LoRA adapters will be frozen, and the xLoRAClassifier is initialized.

    Args:
        load_directory (`str`):
            The directory to load the classifier weights from.
        model (`PreTrainedModel`):
            The model to add the LoRA adapters to. It may be modified in place.
        adapters (`list` or `dict`):
            List of adapter names (the keys of the adapters `dict` in `add_xlora_to_model`) OR Mapping of adapter names to the LoRA adapter id, as per PeftModel.load_adapter. *They will be automatically loaded*, to use as LoRA experts.
            Specify the list if the adapters were trainable.
        verbose (`bool`):
            Display tqdm, total swapping count.
        device (`str`):
            Device of the model, used to load the classifier.
        from_safetensors (`bool`, *optional*, defaults to True):
            Whether to load the classifier weights from a .pt or .safetensors file.
    Returns:
        model (`PeftModel`):
            The new model.
    """

    with open(os.path.join(load_directory, "xlora_config.json"), "r") as f:
        conf = json.load(f)
        conf["device"] = torch.device(device)

        use_trainable_adapters = conf["use_trainable_adapters"]

        xlora_config = xLoRAConfig(**conf)

    if use_trainable_adapters:
        adapters_dict: Dict[str, str] = {name: os.path.join(load_directory, "adapters", name) for name in adapters}
    else:
        assert isinstance(adapters, dict)
        adapters_dict = adapters

    model_peft = add_xlora_to_model(model, xlora_config, adapters_dict, verbose)
    classifier: xLoRAClassifier = model_peft.internal_xlora_classifier  # type: ignore
    if from_safetensors:
        state_dict = safetensors.torch.load_file(  # type: ignore
            os.path.join(load_directory, "xlora_classifier.safetensors"),
            device=device,  # type: ignore
        )
    else:
        state_dict = torch.load(os.path.join(load_directory, "xlora_classifier.pt"))
    classifier.load_state_dict(state_dict)

    return model_peft
