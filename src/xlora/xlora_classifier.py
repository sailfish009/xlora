import typing
from typing import List, Optional, Union

import numpy
import torch
import torch.nn as nn
from peft.peft_model import PeftModel
from transformers.modeling_outputs import (  # type: ignore
    ModelOutput,
)

from .xlora_config import xLoRAConfig

_n_predictions_lifetime: int = 0
_scalings_logging: bool = False


def get_n_predictions_lifetime() -> int:
    global _n_predictions_lifetime
    """
    Reads the n predictions lifetime.
    """
    assert _n_predictions_lifetime is not None
    return _n_predictions_lifetime


def set_n_predictions_lifetime(value: int) -> None:
    global _n_predictions_lifetime
    """
    Sets the n predictions lifetime.
    """
    _n_predictions_lifetime = value


def set_scalings_logging(value: bool):
    global _scalings_logging
    _scalings_logging = value


class xLoRAClassifier(nn.Module):
    """
    A classifier to select LoRA layers for xLoRA. It runs the base model with LoRA adapter scalings of 0.
    """

    def __init__(
        self,
        model: PeftModel,
        config: xLoRAConfig,
        n_classes: int,
        n_layers: int,
    ):
        super().__init__()

        # To avoid registering this with nn.Module
        self.__dict__["model"] = model
        self.n_classes = n_classes
        self.n_layers = n_layers
        self.config = config
        self.log_scalings: List[torch.Tensor] = []

        dtype = next(model.parameters()).dtype


        ##self.score = nn.Linear(config.hidden_size, self.num_labels, bias=False)
        self.inner: nn.ModuleList = nn.ModuleList([])
        if self.config.xlora_depth == 1:
            if config.layerwise_scalings:
                self.last = nn.Linear(config.hidden_size, n_classes * n_layers, bias=False).to(config.device).to(dtype)
            else:
                self.last = nn.Linear(config.hidden_size, n_classes, bias=False).to(config.device).to(dtype)
        elif self.config.xlora_depth == 2:
            self.inner.append(nn.Linear(config.hidden_size, config.xlora_size, bias=False).to(config.device).to(dtype))

            if not config.enable_relu_and_dropout:
                self.inner.append(nn.ReLU())
                self.inner.append(nn.Dropout(p=config.xlora_dropout_p))

            if config.layerwise_scalings:
                self.last = nn.Linear(config.xlora_size, n_classes * n_layers, bias=False).to(config.device).to(dtype)
            else:
                self.last = nn.Linear(config.xlora_size, n_classes, bias=False).to(config.device).to(dtype)
        else:
            assert self.config.xlora_depth > 0
            self.inner.append(nn.Linear(config.hidden_size, config.xlora_size, bias=False).to(config.device).to(dtype))

            if not config.enable_relu_and_dropout:
                self.inner.append(nn.ReLU())
                self.inner.append(nn.Dropout(p=config.xlora_dropout_p))

            for _ in range(config.xlora_depth - 2):
                self.inner.append(
                    nn.Linear(config.xlora_size, config.xlora_size, bias=False).to(config.device).to(dtype)
                )

                if not config.enable_relu_and_dropout:
                    self.inner.append(nn.ReLU())
                    self.inner.append(nn.Dropout(p=config.xlora_dropout_p))

            if config.layerwise_scalings:
                self.last = nn.Linear(config.xlora_size, n_classes * n_layers, bias=False).to(config.device).to(dtype)
            else:
                self.last = nn.Linear(config.xlora_size, n_classes, bias=False).to(config.device).to(dtype)

    def forward(
        self,
        input_ids: Optional[torch.LongTensor] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
        *args,
        **kwargs,
    ) -> torch.Tensor:
        """
        Using the hidden states of the model, predict `n_classes` LoRA alpha values.
        """
        if input_ids is not None:
            batch_size = input_ids.shape[0]
        else:
            batch_size = typing.cast(torch.FloatTensor, inputs_embeds).shape[0]

        # For type checking
        model: PeftModel = self.model  # type: ignore
        with torch.no_grad():
            #model.eval()
            # Disable the xLoRALayers
            with model.disable_adapter(): #peft_model.disable_adapter()
                    # NOTE(EricLBuehler): not implemented
                """
                for module in model.base_model.modules():
                    if isinstance(module.forward.__self__, xLoRALayer):
                        inst = module.forward.__self__
                        inst.disabled = True  # Disable it
                """
    
                kwargs["output_hidden_states"] = True
                kwargs["return_dict"] = True
                result: ModelOutput = model.forward(
                    *args,
                    input_ids=input_ids,
                    inputs_embeds=inputs_embeds,
                    _xlora_classifier_inhibitor_flag=batch_size,  # True,
                    **kwargs,
                )
                #model.train()

            # NOTE(EricLBuehler): not implemented
            """
            # Enable the xLoRALayers
            for module in model.base_model.modules():
                if isinstance(module.forward.__self__, xLoRALayer):
                    inst = module.forward.__self__
                    inst.disabled = False  # Disable it
            """

        #FROM: https://github.com/huggingface/transformers/blob/main/src/transformers/models/mistral/modeling_mistral.py#L1278
        #hidden_states = result[0]
        #[b, length, hidden_size]
        #print ("Hidden_states from result", hidden_states.shape )
        
        
        hidden_states = result.hidden_states  # type: ignore
        
        assert hidden_states is not None
        hidden_state = hidden_states[-1]  # Get the last hidden state
        #hidden_state = hidden_states[0]  # Get the last hidden state
        #hidden_state=result.last_hidden_state

        ##############################################
        #print ("Hidden_state after -1", hidden_state.shape)
        for layer in self.inner:
            hidden_state = layer.forward(hidden_state)

        logits = self.last.forward(hidden_state)
        #self.score = nn.Linear(config.hidden_size, self.num_labels, bias=False)
        #hidden_states = transformer_outputs[0]
        #logits = self.score(hidden_states)
        ##################################
        
        
        #print ("logits ", logits.shape)

        #print ("Pad token id=", self.config.pad_token_id)
        
        if not self.config.layerwise_scalings:
            logits = logits.repeat(1, 1, self.n_layers)
        if input_ids is not None:
            seq_len = input_ids.shape[1]
        else:
            seq_len = typing.cast(torch.FloatTensor, inputs_embeds).shape[1]

        #logits = logits.reshape(batch_size, seq_len, self.n_layers, self.n_classes)

        

        assert attention_mask is not None

        if input_ids is not None:
            ## if no pad token found, use modulo instead of reverse indexing for ONNX compatibility
            # sequence_lengths: Union[int, torch.Tensor] = torch.eq(input_ids, self.config.pad_token_id).int().argmax(-1) - 1
            sequence_lengths: Union[int, torch.Tensor] = torch.eq(attention_mask, 0).int().argmax(-1) - 1
            sequence_lengths = sequence_lengths % input_ids.shape[-1]
            sequence_lengths = sequence_lengths.to(logits.device)  # type: ignore
        else:
            sequence_lengths = -1

        #print ("seq_lens ", sequence_lengths)
        #print ("input_ids ", input_ids)
        #print ("attention_mask ", attention_mask)

        # Get it for the last token
        scalings: torch.Tensor = logits[torch.arange(batch_size, device=logits.device), sequence_lengths]

        
        #print ("scalings before reshape", scalings.shape)

        scalings = scalings.reshape(batch_size,  self.n_layers, self.n_classes)
        #print ("scalings after reshape", scalings.shape)

        if self.config.enable_softmax:
            scalings = scalings.softmax(dim=-1)

        n_pred_life = get_n_predictions_lifetime()
        if n_pred_life > 0:
            print(f"Scaling predictions: {scalings}")
            set_n_predictions_lifetime(n_pred_life - 1)

        return scalings

    def get_nb_trainable_parameters(self):
        # https://github.com/huggingface/peft/blob/main/src/peft/mixed_model.py#L156
        r"""
        Returns the number of trainable parameters and number of all parameters in the model.
        """
        trainable_params = 0
        all_param = 0
        for _, param in self.named_parameters():
            num_params = param.numel()
            # if using DS Zero 3 and the weights are initialized empty
            if num_params == 0 and hasattr(param, "ds_numel"):
                num_params = param.ds_numel  # type: ignore

            # Due to the design of 4bit linear layers from bitsandbytes
            # one needs to multiply the number of parameters by 2 to get
            # the correct number of parameters
            if param.__class__.__name__ == "Params4bit":
                num_params = num_params * 2

            all_param += num_params
            if param.requires_grad:
                trainable_params += num_params

        return trainable_params, all_param

    def flush_log_scalings(self, path: str):
        if not _scalings_logging:
            raise Exception("Scalings logging is disabled!")

        if len(self.log_scalings) == 0:
            raise ValueError("No log scalings to flush.")

        result = torch.cat(self.log_scalings, dim=0)
        npy = result.numpy()
        numpy.save(path, npy)
