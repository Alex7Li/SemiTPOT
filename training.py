
  
# -*- coding: utf-8 -*-
"""Project ML Models.ipynb
Automatically generated by Colaboratory.
Original file is located at
    https://colab.research.google.com/drive/1mcgqfKcW_5BSCRxJmcLxjSZ0FYQkeN3p
"""
import matplotlib.pyplot as plt
from torch.optim import AdamW
import numpy as np
import os
import pandas as pd
import pickle

from transformers import DataCollatorForTokenClassification, AutoModelForTokenClassification, TrainingArguments, Trainer, BertForTokenClassification, AutoTokenizer, get_scheduler
from functools import partial
from tqdm import tqdm as std_tqdm
tqdm = partial(std_tqdm, leave=False, position=0, dynamic_ncols=True)
import torch
from torch.utils.data import DataLoader
from torch.optim import AdamW
import torch.nn as nn
import torch.nn.functional as F
from typing import *
from dataloading_utils import filter_negative_hundred, TransformerCompatDataset, get_num_examples, get_validation_acc, get_dataset_mapping
from ArkDataset.load_ark import load_ark
from TPANNDataset.load_tpann import load_tpann
from TweeBankDataset.load_tweebank import load_tweebank
from AtisDataset.load_atis import load_atis
from GUMDataset.load_GUM import load_gum

device = 'cuda' if torch.cuda.is_available() else 'cpu'
ark_train, ark_val, ark_test = load_ark()
tpann_train, tpann_val, tpann_test = load_tpann()
tweebank_train, tweebank_val, tweebank_test = load_tweebank()
atis_train, atis_val, atis_test = load_atis()
gum_train, gum_val, gum_test = load_gum()

model_names = [
    'gpt2',
    'vinai/bertweet-large',
    'roberta-large',
    'bert-large-cased',
]
dataset_names = [
    'TPANN',
    #'GUM',
    'tweebank',
    #'ark',
    #'atis',
]

def train_epoch(model, train_dataloader, optimizer, scheduler):
    model.train()
    for batch in tqdm(train_dataloader, desc='Training'):
        batch = {k: v.to(device) for k, v in batch.items()}
        outputs = model(**batch)
        loss = outputs.loss
        loss.backward()

        optimizer.step()
        scheduler.step()
        optimizer.zero_grad()

def validation_epoch(model, val_dataloader):
    model.eval()
    preds = []
    labels = []
    for batch in tqdm(val_dataloader, desc='Validation'):
        batch = {k: v.to(device) for k, v in batch.items()}
        batch_labels = batch['labels']
        del batch['labels']
        with torch.no_grad():
            outputs = model(**batch)
    
        logits = outputs.logits
        predictions = torch.argmax(logits, dim=-1)
        preds.append(predictions)
        labels.append(batch_labels)
    return filter_negative_hundred(preds, labels)

def get_dataloader(model_name, dataset, batch_size, shuffle=False):
    tokenizer = AutoTokenizer.from_pretrained(model_name, add_prefix_space=True, use_fast=True)
    if model_name == 'gpt2':
        tokenizer.pad_token = tokenizer.eos_token
    data_collator = DataCollatorForTokenClassification(tokenizer)
    compat_dataset = TransformerCompatDataset(dataset, tokenizer)
    dataloader = DataLoader(compat_dataset, shuffle=shuffle, batch_size=batch_size, collate_fn=data_collator)
    return dataloader

def get_dataset(dataset_name, partition):
    assert partition in {'train', 'val', 'test'}
    if partition == 'train':
        if dataset_name == 'tweebank':
            dataset = tweebank_train
        elif dataset_name == 'ark':
            dataset = ark_train
        elif dataset_name == 'TPANN':
            dataset = tpann_train
        elif dataset_name == "atis":
            dataset = atis_train
        elif dataset_name == "GUM":
            dataset = gum_train
        else:
            raise NotImplementedError
    elif partition == 'val':
        if dataset_name == 'tweebank':
            dataset = tweebank_val
        elif dataset_name == 'ark':
            dataset = ark_val
        elif dataset_name == 'TPANN':
            dataset = tpann_val
        elif dataset_name == "atis":
            dataset = atis_val
        elif dataset_name == "GUM":
            dataset = gum_val
        else:
            raise NotImplementedError
    elif partition == 'test':
        if dataset_name == 'tweebank':
            dataset = tweebank_test
        elif dataset_name == 'ark':
            dataset = ark_test
        elif dataset_name == 'TPANN':
            dataset = tpann_test
        elif dataset_name == "atis":
            dataset = atis_test
        elif dataset_name == "GUM":
            dataset = gum_test
        else:
            raise NotImplementedError
    else:
        raise NotImplementedError
    return dataset

def load_model(model_name, num_labels):
    model = AutoModelForTokenClassification.from_pretrained(model_name, num_labels=num_labels)
    return model.to(device)

def training_loop(model, train_dataloader, val_dataloader, dataset_name, n_epochs, save_path):
    optimizer = AdamW(model.parameters(), lr=5e-5)

    lr_scheduler = get_scheduler(
        name="linear", optimizer=optimizer, num_warmup_steps=0, num_training_steps=get_num_examples(train_dataloader)*n_epochs
    )
    val_accs = []
    torch.save(model.state_dict(), save_path)
    best_val_acc = 0
    for i in tqdm(range(0, n_epochs), desc='Training epochs'):
        train_epoch(model, train_dataloader, optimizer, lr_scheduler)
    
        preds, labels = validation_epoch(model, val_dataloader)
        val_acc = get_validation_acc(preds, labels, dataset_name, dataset_name)
        val_accs.append(val_acc)
        if val_acc > best_val_acc:
            torch.save(model.state_dict(), save_path)
        print(f"Val Accuracy Train epoch {i+1}: {round(100*val_acc,3)}%")
        
    if n_epochs > 1:
        # Make ranadeep happy
        plt.xlabel('epoch')
        plt.ylabel('accuracy')
        plt.ylim([.4, 1])
        plt.title(f"Training curve for model at {save_path}")
        plt.plot(range(n_epochs), val_accs)
        plt.show()
    model.load_state_dict(torch.load(save_path))  
    return model

def pipeline(hparams):
    torch.cuda.empty_cache()
    train_dataset = get_dataset(hparams['dataset'], 'train')
    train_dataloader = get_dataloader(hparams['model_name'], train_dataset, hparams['batch_size'])
    val_dataset = get_dataset(hparams['dataset'], 'val')
    val_dataloader = get_dataloader(hparams['model_name'], val_dataset, hparams['batch_size'])
    num_labels = train_dataset.num_labels
    n_epochs = hparams['n_epochs']
    model = load_model(hparams['model_name'], num_labels)
    return training_loop(model, train_dataloader, val_dataloader, hparams['dataset'], n_epochs, hparams['save_path'])


def run_experiment():
    result_dict = dict()
    for model_name in model_names:

        result_dict[model_name] = dict()

        for train_dataset_name in dataset_names:

            result_dict[model_name][train_dataset_name] = dict()

            hparams = {
                'n_epochs': 4,
                'batch_size': 32,
                'dataset': train_dataset_name,
                'model_name': model_name,
            }
            if not os.path.exists('models'):
                os.mkdir('models')
            hparams['save_path'] = os.path.join('models', hparams['model_name'].split('/')[-1] + "_" + hparams['dataset'])

            print(f"Training on: {train_dataset_name}, with model: {model_name}")
            trained_model = pipeline(hparams)
            
            for test_dataset_name in dataset_names:
                print(f"Validating: {test_dataset_name}, with model: {model_name}, trained on: {train_dataset_name}")
                val_dataset = get_dataset(hparams['dataset'], 'val')
                val_dataloader = get_dataloader(hparams['model_name'], val_dataset, hparams['batch_size'])
                preds, labels = validation_epoch(trained_model, val_dataloader)
                acc = get_validation_acc(preds, labels,  train_dataset_name, test_dataset_name)
                print(f"Test Accuracy on {test_dataset_name}: {round(100*acc,3)}%")
                result_dict[model_name][train_dataset_name][test_dataset_name] = 100*acc

    return result_dict

def main():  
    print("Device: ", device)
    results = run_experiment()
    # results = {'gpt2': {'tweebank': {'tweebank': 88.605, 'TPANN': 70.083, 'ark': 55.007}, 'TPANN': {'tweebank': 62.676, 'TPANN': 86.848, 'ark': 58.427}, 'ark': {'tweebank': 47.659, 'TPANN': 64.866, 'ark': 86.512}}, 'vinai/bertweet-large': {'tweebank': {'tweebank': 93.93, 'TPANN': 72.831, 'ark': 58.798}, 'TPANN': {'tweebank': 67.468, 'TPANN': 93.794, 'ark': 63.418}, 'ark': {'tweebank': 50.942, 'TPANN': 68.416, 'ark': 93.649}}, 'roberta-large': {'tweebank': {'tweebank': 92.997, 'TPANN': 71.411, 'ark': 57.999}, 'TPANN': {'tweebank': 71.133, 'TPANN': 92.837, 'ark': 63.047}, 'ark': {'tweebank': 52.045, 'TPANN': 67.397, 'ark': 92.689}}, 'bert-large-cased': {'tweebank': {'tweebank': 91.77, 'TPANN': 68.13, 'ark': 53.976}, 'TPANN': {'tweebank': 65.373, 'TPANN': 90.571, 'ark': 60.942}, 'ark': {'tweebank': 50.051, 'TPANN': 63.793, 'ark': 90.931}}}
    print(results)
    with open('model_out.pkl', 'wb') as f:
        pickle.dump(results, f)


if __name__ == '__main__':
    main()# -*- coding: utf-8 -*-