"""
Let's try to make a model that generates pseudo labels.

1) Train a model teacher_model on labelled dataset A.

2) Apply teacher_model to unlabelled dataset B and save all predictions
with > 90% confidence as psuedo labels to generate labeled dataset C

4) Train a model student_model on combined dataset A + C

Interesting references
https://openreview.net/forum?id=-ODN6SbiUU

"""
import training
import os
import numpy as np
import torch
from tqdm import tqdm
import pickle
import torch.utils.data
from torch.optim import AdamW   
from transformers import AutoTokenizer
from dataloading_utils import get_validation_acc

batch_size = 16
class PseudoDataset(torch.utils.data.Dataset):
    def __init__(self, pseudolabel_path): 
        loaded = np.load(pseudolabel_path, allow_pickle=True)
        self.inputs = loaded['inputs']
        self.labels = loaded['labels']
          
        assert(len(self.inputs) == len(self.labels))
        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'

    def __len__(self):
        return len(self.inputs)

    def __getitem__(self, ind):
        X = self.inputs[ind]
        Y = self.labels[ind]
        Y = torch.as_tensor(Y, dtype=torch.long, device=self.device) 
        return X, Y

class MergedDataset(torch.utils.data.Dataset):
    def __init__(self, dataset1, dataset2): 
      self.dataset1 = dataset1
      self.dataset2 = dataset2
      self.L1 = len(self.dataset1)
      self.L2 = len(self.dataset2)

    def __len__(self):
        return self.L1 + self.L2

    def __getitem__(self, ind):
      if ind < self.L1:
        return self.dataset1[ind]
      else:
        return self.dataset2[ind - self.L1]


def train_teacher(model_name, dataset_name, save_path):
  hparams = {
      'n_epochs': 10,
      'batch_size': batch_size,
      'dataset': dataset_name,
      'model_name': model_name,
      'save_path': save_path,
  }
  training.pipeline(hparams)

def get_x_and_pseudolabels(model, dataset, model_name):
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    dataloader = training.get_dataloader(model_name, dataset, shuffle=False, batch_size=batch_size)
    tokenizer = AutoTokenizer.from_pretrained(model_name, add_prefix_space=True, use_fast=True)
    model.eval()
    inputs = []
    pseudolabels = []
    offset = 0
    for batch in tqdm(dataloader, desc='generating psuedolabels'):
      batch = {k: v.to(device) for k, v in batch.items()}
      del batch['labels']
      with torch.no_grad():
          outputs = model(**batch)
  
      predictions = torch.argmax(outputs.logits, dim=-1)
      probabilities = torch.nn.Softmax(dim=2)(outputs.logits)
      confidence = torch.zeros_like(predictions, dtype=torch.float)
      for element_ind in range(predictions.shape[0]):
        for seq_ind in range(predictions.shape[1]):
          confidence[element_ind][seq_ind] = probabilities[element_ind][seq_ind][predictions[element_ind][seq_ind]]
  
      predictions = predictions.detach().cpu().numpy()
      confidence = confidence.detach().cpu().numpy()
      for i in range(batch['input_ids'].shape[0]):
        x, _ = dataset[i + offset]
        tokenized_inputs = tokenizer(x, truncation=True, is_split_into_words=True)
        word_ids = tokenized_inputs.word_ids()
        prev_word_id = -1
        seq_pseudo_labels = []
        for seq_ind in range(len(word_ids)):
          word_id = word_ids[seq_ind]
          if confidence[i][seq_ind] > .9 and word_id is not None and prev_word_id != word_id:
            seq_pseudo_labels.append(predictions[i][seq_ind])
          else:
            seq_pseudo_labels.append(-100)
        pseudolabels.append(seq_pseudo_labels)
        inputs.append(x)
      offset += batch['input_ids'].shape[0]
    return inputs, pseudolabels

def generate_pseudo_labels(teacher_path, teacher_model_name, teacher_n_labels, dataset_name, save_path):
  teacher = training.load_model(teacher_model_name, teacher_n_labels)
  teacher.load_state_dict(torch.load(teacher_path))
  unsupervised_dataset = training.get_dataset(dataset_name, 'train')
  inputs, psuedo_labels = get_x_and_pseudolabels(teacher, unsupervised_dataset, teacher_model_name)

  np.savez(save_path, inputs=inputs, labels=psuedo_labels)
  return save_path

def train_on_psuedolabels(model_name, pseudolabel_path, base_dataset_name, save_path):
  dataset1 = training.get_dataset(base_dataset_name, 'train')
  dataset2 = PseudoDataset(pseudolabel_path)
  dataset = MergedDataset(dataset1, dataset2)
  train_dataloader = training.get_dataloader(model_name, dataset, batch_size, shuffle=True)
  val_dataset = training.get_dataset(base_dataset_name, 'val')
  val_dataloader = training.get_dataloader(model_name, val_dataset, batch_size, shuffle=False)
  model = training.load_model(model_name, dataset1.num_labels)
  n_epochs = 4
  training.training_loop(model, train_dataloader, val_dataloader, base_dataset_name, n_epochs, save_path)
  return save_path

def validate_student(model_name, trained_student_path, train_dataset_name, train_dataset_n_labels, val_dataset_name):
    student = training.load_model(model_name, train_dataset_n_labels)
    student.load_state_dict(torch.load(trained_student_path))
    val_dataset = training.get_dataset(val_dataset_name, 'test')
    val_dataloader = training.get_dataloader(model_name, val_dataset, batch_size, shuffle=False)
    preds, labels = training.validation_epoch(student, val_dataloader)
    test_acc = get_validation_acc(preds, labels, train_dataset_name, val_dataset_name)
    return test_acc

def run_pseudolabel_experiment():
  teacher_model_name = 'bert-large-cased'
  student_model_name = 'bert-large-cased'
  result_dict = dict()
  result_dict[student_model_name] = dict()
  for supervised_dataset_name in training.dataset_names:
    result_dict[student_model_name][supervised_dataset_name] = dict()

  for supervised_dataset_name in training.dataset_names:
    teacher_model_path = os.path.join('models', "teacher_" + str(teacher_model_name.split('/')[-1]) + "_" + supervised_dataset_name)
    supervised_dataset_n_labels = training.get_dataset(supervised_dataset_name, 'train').num_labels


    if not os.path.exists(teacher_model_path):
      train_teacher(teacher_model_name, supervised_dataset_name, teacher_model_path)
      print(f"Done training. Saved to {teacher_model_path}")
    else:
      print("teach model path exists")
  
    for unsupervised_dataset_name in training.dataset_names:
      if unsupervised_dataset_name != supervised_dataset_name:
        pseudolabel_path = os.path.join('pseudolabels', str(teacher_model_name.split('/')[-1]) + "_" + supervised_dataset_name + "_" + unsupervised_dataset_name + ".npz")
        student_model_path = os.path.join('models', "student_" + str(student_model_name.split('/')[-1]) + "_" + supervised_dataset_name + "_" + unsupervised_dataset_name + ".npz")
        if not os.path.exists(pseudolabel_path) and not os.path.exists(student_model_path):
          generate_pseudo_labels(teacher_model_path, teacher_model_name,\
            supervised_dataset_n_labels, unsupervised_dataset_name, pseudolabel_path)
          print(f"Generated Pseudolabels. Saved to {pseudolabel_path}")

        if not os.path.exists(student_model_path):
          train_on_psuedolabels(student_model_name, pseudolabel_path, supervised_dataset_name, student_model_path)
          print(f"Student has been trained, saved to {student_model_path}")

        test_acc = validate_student(student_model_name, student_model_path,\
            supervised_dataset_name, supervised_dataset_n_labels, unsupervised_dataset_name)
        # I'm running out of disk space O:
        os.remove(pseudolabel_path)
      else:
        test_acc = validate_student(student_model_name, teacher_model_path,\
            supervised_dataset_name, supervised_dataset_n_labels, unsupervised_dataset_name)
      print(f"{student_model_name} trained with teacher {teacher_model_name} on {supervised_dataset_name}"
            f"has accuracy {test_acc * 100:.2f}% on {unsupervised_dataset_name}")
      result_dict[student_model_name][supervised_dataset_name][unsupervised_dataset_name] = 100 * test_acc

  return result_dict

def main():
  if not os.path.exists('pseudolabels'):
      os.mkdir('pseudolabels')
  if not os.path.exists('models'):
      os.mkdir('models')
  results = run_pseudolabel_experiment(); 
  print(results)
  with open('psuedolabel_out.pkl', 'wb') as f:
      pickle.dump(results, f)

if __name__ == '__main__':
  main()

