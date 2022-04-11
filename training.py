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

device = 'cuda' if torch.cuda.is_available() else 'cpu'
ark_train, ark_val, ark_test = load_ark()
tpann_train, tpann_val, tpann_test = load_tpann()
tweebank_train, tweebank_val, tweebank_test = load_tweebank()
model_names = [
    'gpt2',
    'vinai/bertweet-large',
    'roberta-large',
    'bert-large-cased',
]
dataset_names = [
    'tweebank',
    'TPANN',
    'ark'
]

def train_epoch(model, train_dataloader, optimizer, scheduler):
    model.train()
    for batch in tqdm(train_dataloader):
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
    for batch in tqdm(val_dataloader):
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

def get_dataset(model_name, dataset_name, batch_size, partition):
    assert partition in {'train', 'val', 'test'}
    tokenizer = AutoTokenizer.from_pretrained(model_name, add_prefix_space=True, use_fast=True)
    if model_name == 'gpt2':
        tokenizer.pad_token = tokenizer.eos_token
    data_collator = DataCollatorForTokenClassification(tokenizer)
    if partition == 'train':
        if dataset_name == 'tweebank':
            dataset = tweebank_train
        elif dataset_name == 'ark':
            dataset = ark_train
        elif dataset_name == 'TPANN':
            dataset = tpann_train
        else:
            raise NotImplementedError
    elif partition == 'val':
        if dataset_name == 'tweebank':
            dataset = tweebank_val
        elif dataset_name == 'ark':
            dataset = ark_val
        elif dataset_name == 'TPANN':
            dataset = tpann_val
        else:
            raise NotImplementedError
    elif partition == 'test':
        if dataset_name == 'tweebank':
            dataset = tweebank_test
        elif dataset_name == 'ark':
            dataset = ark_test
        elif dataset_name == 'TPANN':
            dataset = tpann_test
        else:
            raise NotImplementedError
    else:
        raise NotImplementedError
    dataloader = DataLoader(TransformerCompatDataset(dataset, tokenizer), shuffle=False, batch_size=batch_size, collate_fn=data_collator)
    return dataloader, dataset.num_labels

def load_model(model_name, num_labels):
    model = AutoModelForTokenClassification.from_pretrained(model_name, num_labels=num_labels)
    return model.to(device)

def training_loop(hparams):
    torch.cuda.empty_cache()
    train_dataloader, num_labels = get_dataset(hparams['model_name'], hparams['dataset'], hparams['batch_size'], 'train')
    val_dataloader, _ = get_dataset(hparams['model_name'], hparams['dataset'], hparams['batch_size'], 'val')
    n_epochs = hparams['n_epochs']
    model = load_model(hparams['model_name'], num_labels)
    optimizer = AdamW(model.parameters(), lr=5e-5)

    lr_scheduler = get_scheduler(
        name="linear", optimizer=optimizer, num_warmup_steps=0, num_training_steps=get_num_examples(train_dataloader)*n_epochs
    )
    val_accs = []
    if not os.path.exists('models'):
        os.mkdir('models')
    best_model_path = os.path.join('models', hparams['model_name'] + "_" + hparams['dataset'])
    torch.save(model.state_dict(), best_model_path)
    best_val_acc = 0
    for i in tqdm(range(0, n_epochs)):
        train_epoch(model, train_dataloader, optimizer, lr_scheduler)
    
        preds, labels = validation_epoch(model, val_dataloader)
        val_acc = get_validation_acc(preds, labels,  hparams["dataset"], hparams['dataset'])
        val_accs.append(val_acc)
        if val_acc > best_val_acc:
            torch.save(model.state_dict(), best_model_path)
        print(f"Val Accuracy Train epoch {i+1}: {round(100*val_acc,3)}%")
        
    if hparams['n_epochs'] > 1:
        # Make ranadeep happy
        plt.xlabel('epoch')
        plt.ylabel('accuracy')
        plt.ylim([.4, 1])
        plt.title(f"Training curve for: {hparams['model_name']}")
        plt.plot(range(n_epochs), val_accs)
        plt.show()
    model.load_state_dict(torch.load(best_model_path))  
    return model
def run_experiment():
    result_dict = dict()
    for model_name in model_names:

        result_dict[model_name] = dict()

        for train_dataset_name in dataset_names:

            result_dict[model_name][train_dataset_name] = dict()

            hparams = {
                'n_epochs': 10,
                'batch_size': 8,
                'dataset': train_dataset_name,
                'model_name': model_name
            }

            print(f"Training on: {train_dataset_name}, with model: {model_name}")
            trained_model = training_loop(hparams)
            
            for test_dataset_name in dataset_names:
                print(f"Validating: {test_dataset_name}, with model: {model_name}, trained on: {train_dataset_name}")
                val_dataloader, _ = get_dataset(model_name, test_dataset_name, hparams['batch_size'], 'test')
                preds, labels = validation_epoch(trained_model, val_dataloader)
                acc = get_validation_acc(preds, labels,  train_dataset_name, test_dataset_name)
                print(f"Test Accuracy on {test_dataset_name}: {round(100*acc,3)}%")
                result_dict[model_name][train_dataset_name][test_dataset_name] = round(100*acc,3)

    return result_dict

def main():  
    print("Device: ", device)
    results = run_experiment()
    # results = {'gpt2': {'tweebank': {'tweebank': 88.605, 'TPANN': 70.083, 'ark': 55.007}, 'TPANN': {'tweebank': 62.676, 'TPANN': 86.848, 'ark': 58.427}, 'ark': {'tweebank': 47.659, 'TPANN': 64.866, 'ark': 86.512}}, 'vinai/bertweet-large': {'tweebank': {'tweebank': 93.93, 'TPANN': 72.831, 'ark': 58.798}, 'TPANN': {'tweebank': 67.468, 'TPANN': 93.794, 'ark': 63.418}, 'ark': {'tweebank': 50.942, 'TPANN': 68.416, 'ark': 93.649}}, 'roberta-large': {'tweebank': {'tweebank': 92.997, 'TPANN': 71.411, 'ark': 57.999}, 'TPANN': {'tweebank': 71.133, 'TPANN': 92.837, 'ark': 63.047}, 'ark': {'tweebank': 52.045, 'TPANN': 67.397, 'ark': 92.689}}, 'bert-large-cased': {'tweebank': {'tweebank': 91.77, 'TPANN': 68.13, 'ark': 53.976}, 'TPANN': {'tweebank': 65.373, 'TPANN': 90.571, 'ark': 60.942}, 'ark': {'tweebank': 50.051, 'TPANN': 63.793, 'ark': 90.931}}}
    print(results)
    with open('model_out.pkl', 'wb') as f:
        pickle.dump(results, f)


if __name__ == '__main__':
    main()