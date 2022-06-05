
  
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
import itertools
from pseudolabels import MergedDataset
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
from augmented_datasets import ArkAugDataset,TPANNAugDataset,AtisAugDataset,GUMAugDataset,TweebankAugTrain,get_augmented_dataloader,generate_mask_and_data
def download_datasets():
    import nltk
    nltk.data.path.append('/home/ubuntu/SemiTPOT/nltk_data')
    nltk.download('punkt')
    nltk.download('wordnet')
    from nltk.corpus import wordnet as wn
    import spacy
from augmented_datasets import get_augmented_dataloader

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
    'tweebank',
    'TPANN',
    'GUM',
    #'ark',
    #'atis',
]

def train_epoch(model, train_dataloader, optimizer, scheduler):
    model.train()
    for batch in tqdm(train_dataloader, desc='Training'):
        batch = {k: v.to(device) for k, v in batch.items()}
        outputs = model(**batch)
        loss = outputs.loss
        # print("loss type: ", type(loss))
        # print("loss shape: ", loss.shape)
        # input("")
        loss.backward()

        optimizer.step()
        scheduler.step()
        optimizer.zero_grad()

def train_epoch_aug(model, train_dataloader, optimizer, scheduler):
    model.train()
    mse_loss = nn.MSELoss()
    eps = 1e-4
    lambda_ = 1
    mu = 25
    nu = 1
    for batch in tqdm(train_dataloader, desc='Training'):
        batch = {k: v.to(device) for k, v in batch.items()}
        
        batch_input_ids, batch_input_ids_aug,mid = generate_mask_and_data(batch["input_ids"],"input",mid=None)
        
        batch_attention_mask, batch_attention_mask_aug,_= generate_mask_and_data(batch["attention_mask"],"attention",mid=mid)
        batch_labels,batch_labels_aug,_ = generate_mask_and_data(batch["labels"],"labels",mid=mid)
        
        batch_normal = {"input_ids":batch_input_ids,"attention_mask":batch_attention_mask,"labels":batch_labels}
        batch_aug = {"input_ids":batch_input_ids_aug,"attention_mask":batch_attention_mask_aug,"labels":batch_labels_aug}
        
        
        z_normal = model(**batch_normal)
        z_aug = model(**batch_aug)
        z_normal_cross_entropy = z_normal.loss
        z_aug_cross_entropy = z_aug.loss

        
        cross_entropy_loss = z_normal_cross_entropy + z_aug_cross_entropy
        
        
        #Invariance loss [Done]
        sim_loss = mse_loss(z_normal.logits,z_aug.logits)
        z_normal_cross_entropy+=(sim_loss/2)*lambda_
        z_aug_cross_entropy+=(sim_loss/2)*lambda_
        
        #Variance loss [Done]
        std_z_a = torch.sqrt(z_aug.logits.var(dim=0)+eps)
        std_z_b = torch.sqrt(z_normal.logits.var(dim=0)+eps)
        std_loss = torch.mean(nn.functional.relu(1-std_z_a)) + torch.mean(nn.functional.relu(1-std_z_b))
        
        z_normal_cross_entropy+=(std_loss/2)*mu
        z_aug_cross_entropy+=(std_loss/2)*mu
        
        
        #Covariance loss[Done]
        z_normal = z_normal.logits - z_normal.logits.mean(0) #B X words X Dim
        z_aug = z_aug.logits - z_aug.logits.mean(0)

        cov_z_a = torch.bmm(z_aug, torch.transpose(z_aug,1,2))/batch_normal["input_ids"].shape[0]
        cov_z_b = torch.bmm(z_normal,torch.transpose(z_normal,1,2))/batch_normal["input_ids"].shape[0]
        cov_loss = (torch.sum(cov_z_a) - torch.diagonal(cov_z_a,0).pow(2).sum()) + (torch.sum(cov_z_b) - torch.diagonal(cov_z_b,0).pow(2).sum())
        cov_loss = cov_loss / z_normal.shape[1]

        z_normal_cross_entropy+=(cov_loss/2)*nu
        z_aug_cross_entropy+=(cov_loss/2)*nu



        z_normal_cross_entropy.backward(retain_graph=True)
        z_aug_cross_entropy.backward()
        
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

def validation_epoch_aug(model, val_dataloader):
    model.eval()
    preds_normal = []
    preds_aug = []
    labels = []
    for batch in tqdm(val_dataloader, desc='Validation'):
        batch = {k: v.to(device) for k, v in batch.items()}
        batch_labels = batch['labels']
        del batch['labels']
        batch_input_ids, batch_input_ids_aug,mid = generate_mask_and_data(batch["input_ids"],"input",mid=None)
        batch_attention_mask, batch_attention_mask_aug,_= generate_mask_and_data(batch["attention_mask"],"attention",mid=mid)
        batch_labels,batch_labels_aug,_ = generate_mask_and_data(batch_labels,"labels",mid=mid)
        batch_normal = {"input_ids":batch_input_ids,"attention_mask":batch_attention_mask,"labels":batch_labels}
        batch_aug = {"input_ids":batch_input_ids_aug,"attention_mask":batch_attention_mask_aug,"labels":batch_labels_aug}
        with torch.no_grad():
            outputs_normal = model(**batch_normal)
            outputs_aug = model(**batch_aug)
    
        logits_normal = outputs_normal.logits
        predictions_normal = torch.argmax(logits_normal, dim=-1)
        preds_normal.append(predictions_normal)

        logits_aug = outputs_aug.logits
        predictions_aug = torch.argmax(logits_aug, dim=-1)
        preds_aug.append(predictions_aug)

        labels.append(batch_labels)
        
    preds_normal , ignored_labels = filter_negative_hundred(preds_normal, labels)
    preds_aug, labels = filter_negative_hundred(preds_aug, labels)
    assert len(preds_normal) ==len(preds_aug)==len(labels)
    return preds_normal,preds_aug,labels

def get_dataloader(model_name:str, dataset, batch_size, shuffle=False):
    tokenizer = AutoTokenizer.from_pretrained(model_name, add_prefix_space=True, use_fast=True, model_max_length=512)
    if model_name == 'gpt2':
        tokenizer.pad_token = tokenizer.eos_token
    data_collator = DataCollatorForTokenClassification(tokenizer)
    compat_dataset = TransformerCompatDataset(dataset, tokenizer)
    dataloader = DataLoader(compat_dataset, shuffle=shuffle, batch_size=batch_size, collate_fn=data_collator)
    return dataloader



def get_dataset(dataset_name, partition):
    assert partition in {'train', 'val', 'test', 'all'}
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
    elif partition == 'all':
        return MergedDataset(
            get_dataset(dataset_name, 'train'),
            get_dataset(dataset_name, 'val'),
            get_dataset(dataset_name, 'test')
        )
    else:
        raise NotImplementedError
    return dataset

def get_augmented_dataset(train_X,train_Y):
  augmented_examples = []
  augmented_labels = []
  augment_percent = .1
  for i,sentence in enumerate(train_X):
    break1 = False
    breaktonextword = False
    break2 = False
    break3 = False
    augmented = False
    change_made = False
    ex = sentence.copy()
    num_words_to_augment = max(1,int(augment_percent*(len(sentence))))
    for j,word in enumerate(sentence):
        temp_pos = ""
        if train_Y[i][j] in ["V","N","A","VB","VBD","VBG","VBN","VBP",
                            "VBZ","JJ","JJS","JJR","NN","NNS","ADJ","NOUN","VERB"]:
            if train_Y[i][j] in ["VB","VBD","VBG","VBN","VBP","VBZ","VERB"]:
                temp_pos = "V"

            if train_Y[i][j] in ["JJ","JJS","JJR","ADJ"]:
                temp_pos = "A"
            if train_Y[i][j] in ["NN","NNS","NOUN"]:
                temp_pos = "N"
            
            for s,synset in enumerate(wn.synsets(train_X[i][j])):
                if s==0:
                    continue
                if synset.pos() == train_Y[i][j].lower() or synset.pos() ==temp_pos.lower():
                    for lemma in synset.lemmas():
                        ex[j] = lemma.name()
                        change_made=True
                        num_words_to_augment-=1
                        if num_words_to_augment ==0:
                            augmented_examples.append([ex,train_X[i]])
                            augmented_labels.append(train_Y[i])
                            break1 = True
                            augmented = True
                            break
                        elif j==(len(sentence)-1) and change_made:
                            augmented_examples.append([ex,train_X[i]])
                            augmented_labels.append(train_Y[i])
                            augmented = True
                            break1 = True
                            break
                        elif num_words_to_augment>0:
                            breaktonextword = True
                            break
                if breaktonextword:
                    breaktonextword = False
                    break
                if break1:
                    break1 = False
                    break2 = True
                    break
            if break2:
                break2 = False
                break
        
  return augmented_examples, augmented_labels
# print("test aug datasets: \n")
# augmented_tweebank_train_dataloader = get_augmented_dataloader(dataset="tweebank",partition="train",model="gpt2")
# augmented_tpann_train_dataloader = get_augmented_dataloader(dataset="tpann",partition="train",model="gpt2")
# for batch in augmented_tpann_train_dataloader:
#     print("tpann")
#     print(batch,"\n")
#     print(batch["input_ids"].shape)
#     print(batch["attention_mask"].shape)
#     print(batch["labels"].shape)
#     break
# augmented_gum_train_dataloader = get_augmented_dataloader(dataset="gum",partition="train",model="gpt2")
# for batch in augmented_gum_train_dataloader:
#     print("gum")
#     print(batch,"\n")
#     print(batch["input_ids"].shape)
#     print(batch["attention_mask"].shape)
#     print(batch["labels"].shape)
#     break
# input("")
# print("data loaded correctly!")
# input("")

def load_model(model_name, num_labels):
    model = AutoModelForTokenClassification.from_pretrained(model_name, num_labels=num_labels)
    return model.to(device)
def training_loop_aug(model, train_dataloader, val_dataloader, dataset_name, n_epochs, save_path):
    optimizer = AdamW(model.parameters(), lr=5e-5)

    lr_scheduler = get_scheduler(
        name="linear", optimizer=optimizer, num_warmup_steps=0, num_training_steps=len(train_dataloader)*n_epochs
    )
    val_accs = []
    torch.save(model.state_dict(), save_path)
    best_val_acc = 0
    for i in tqdm(range(0, n_epochs), desc='Training epochs'):
        for batch in train_dataloader:
            batch = {k: v.to(device) for k, v in batch.items()}
        train_epoch_aug(model, train_dataloader, optimizer, lr_scheduler)
    
        preds_normal,preds_aug, labels = validation_epoch_aug(model, val_dataloader)
        val_acc_normal = get_validation_acc(preds_normal, labels, dataset_name, dataset_name)
        val_acc_aug = get_validation_acc(preds_aug, labels, dataset_name, dataset_name)
        print("val acc normal: ", val_acc_normal)
        print("val acc aug: ", val_acc_aug,"\n")
        
        val_acc = (val_acc_normal + val_acc_aug) / 2
        val_accs.append((val_acc_normal + val_acc_aug) / 2 )
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


def training_loop(model, train_dataloader, val_dataloader, dataset_name, n_epochs, save_path, aug=False):
    optimizer = torch.optim.NAdam(model.parameters(), lr=3e-5, weight_decay=1e-4)

    lr_scheduler = get_scheduler(
        name="linear", optimizer=optimizer, num_warmup_steps=0, num_training_steps=len(train_dataloader)*n_epochs
    )
    val_accs = []
    torch.save(model.state_dict(), save_path)
    best_val_acc = 0
    for i in tqdm(range(0, n_epochs), desc='Training epochs'):
        if aug:
            for batch in train_dataloader:
                batch = {k: v.to(device) for k, v in batch.items()}
            print("entering train epoch aug: \n")
            train_epoch_aug(model, train_dataloader, optimizer, lr_scheduler) # added
        else:
            train_epoch(model, train_dataloader, optimizer, lr_scheduler)

        preds, labels = validation_epoch(model, val_dataloader)
        val_acc = get_validation_acc(preds, labels, dataset_name, dataset_name)
        val_accs.append(val_acc)
        if val_acc > best_val_acc:
            torch.save(model.state_dict(), save_path)
        print(f"Val Accuracy Train epoch {i+1}: {round(100*val_acc,3)}%")
        if val_acc < .2:
            print(f"Model collapsed, restarting from last epoch.")
            model.load_state_dict(torch.load(save_path))  

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

def pipeline(hparams,run_aug=False):
    torch.cuda.empty_cache()
    train_dataset = get_dataset(hparams['dataset'], 'train')
    train_dataloader = get_dataloader(hparams['model_name'], train_dataset, hparams['batch_size'])
    val_dataset = get_dataset(hparams['dataset'], 'val')
    val_dataloader = get_dataloader(hparams['model_name'], val_dataset, hparams['batch_size'])
    num_labels = train_dataset.num_labels
    n_epochs = hparams['n_epochs']
    model = load_model(hparams['model_name'], num_labels)
    if run_aug:
        train_dataloader = get_augmented_dataloader(dataset=hparams['dataset'],partition="train",model="gpt2") # added
        for batch in train_dataloader:
            batch = {k: v.to(device) for k, v in batch.items()}
        val_dataloader = get_augmented_dataloader(dataset=hparams['dataset'],partition="dev",model="gpt2") # added
        for batch in val_dataloader:
            batch = {k: v.to(device) for k, v in batch.items()}
        return training_loop_aug(model, train_dataloader, val_dataloader, hparams['dataset'], n_epochs, hparams['save_path'])
    return training_loop(model, train_dataloader, val_dataloader, hparams['dataset'], n_epochs, hparams['save_path'])


def run_experiment(run_aug=False):
    result_dict = dict()
    for model_name in model_names:

        result_dict[model_name] = dict()

        for train_dataset_name in dataset_names:

            result_dict[model_name][train_dataset_name] = dict()

            hparams = {
                'n_epochs': 10,
                'batch_size': 32,
                'dataset': train_dataset_name,
                'model_name': model_name,
            }
            if not os.path.exists('models'):
                os.mkdir('models')
            hparams['save_path'] = os.path.join('models', hparams['model_name'].split('/')[-1] + "_" + hparams['dataset'])
            print(f"Training on: {train_dataset_name}, with model: {model_name}")
            if run_aug:
                trained_model = pipeline(hparams,run_aug=True)
            else:
                trained_model = pipeline(hparams)
            for test_dataset_name in dataset_names:
                print(f"Validating: {test_dataset_name}, with model: {model_name}, trained on: {train_dataset_name}")
                val_dataset = get_dataset(test_dataset_name, 'val')
                val_dataloader = get_dataloader(hparams['model_name'], val_dataset, hparams['batch_size'])
                preds, labels = validation_epoch(trained_model, val_dataloader)
                acc = get_validation_acc(preds, labels,  train_dataset_name, test_dataset_name)
                print(f"Test Accuracy on {test_dataset_name}: {round(100*acc,3)}%")
                result_dict[model_name][train_dataset_name][test_dataset_name] = 100*acc
                torch.cuda.empty_cache()

    return result_dict

def main():  
    print("Device: ", device)
    run_aug = False
    if run_aug:
        download_datasets()
    results = run_experiment(run_aug=run_aug)
    # results = {'gpt2': {'tweebank': {'tweebank': 88.605, 'TPANN': 70.083, 'ark': 55.007}, 'TPANN': {'tweebank': 62.676, 'TPANN': 86.848, 'ark': 58.427}, 'ark': {'tweebank': 47.659, 'TPANN': 64.866, 'ark': 86.512}}, 'vinai/bertweet-large': {'tweebank': {'tweebank': 93.93, 'TPANN': 72.831, 'ark': 58.798}, 'TPANN': {'tweebank': 67.468, 'TPANN': 93.794, 'ark': 63.418}, 'ark': {'tweebank': 50.942, 'TPANN': 68.416, 'ark': 93.649}}, 'roberta-large': {'tweebank': {'tweebank': 92.997, 'TPANN': 71.411, 'ark': 57.999}, 'TPANN': {'tweebank': 71.133, 'TPANN': 92.837, 'ark': 63.047}, 'ark': {'tweebank': 52.045, 'TPANN': 67.397, 'ark': 92.689}}, 'bert-large-cased': {'tweebank': {'tweebank': 91.77, 'TPANN': 68.13, 'ark': 53.976}, 'TPANN': {'tweebank': 65.373, 'TPANN': 90.571, 'ark': 60.942}, 'ark': {'tweebank': 50.051, 'TPANN': 63.793, 'ark': 90.931}}}
    print(results)
    with open('model_out.pkl', 'wb') as f:
        pickle.dump(results, f)


if __name__ == '__main__':
    main()# -*- coding: utf-8 -*-
