import random
from pathlib import Path
import numpy as np
from sklearn.model_selection import train_test_split

import torch
from torch.utils.data import DataLoader
from transformers import BertTokenizerFast
from transformers import BertForSequenceClassification, AdamW

from patrickstar.runtime import initialize_engine

import os
os.environ["RANK"] = "0"
os.environ["WORLD_SIZE"] = "1"
os.environ["MASTER_ADDR"] = "127.0.0.1"
os.environ["MASTER_PORT"] = "29500"
torch.distributed.init_process_group(backend='nccl')

def read_imdb_split(split_dir):
    split_dir = Path(split_dir)
    texts = []
    labels = []
    for label_dir in ["pos", "neg"]:
        for text_file in (split_dir/label_dir).iterdir():
            texts.append(text_file.read_text())
            labels.append(0 if label_dir is "neg" else 1)

    return texts, labels

# wget http://ai.stanford.edu/~amaas/data/sentiment/aclImdb_v1.tar.gz
# tar -xf aclImdb_v1.tar.gz
train_texts, train_labels = read_imdb_split('aclImdb/train')
test_texts, test_labels = read_imdb_split('aclImdb/test')
train_texts, val_texts, train_labels, val_labels = train_test_split(train_texts, train_labels, test_size=.2)

tokenizer = BertTokenizerFast.from_pretrained('bert-base-uncased')

train_encodings = tokenizer(train_texts, truncation=True, padding=True)
val_encodings = tokenizer(val_texts, truncation=True, padding=True)
test_encodings = tokenizer(test_texts, truncation=True, padding=True)

class IMDbDataset(torch.utils.data.Dataset):
    def __init__(self, encodings, labels):
        self.encodings = encodings
        self.labels = labels

    def __getitem__(self, idx):
        item = {key: torch.tensor(val[idx]) for key, val in self.encodings.items()}
        item['labels'] = torch.tensor(self.labels[idx])
        return item

    def __len__(self):
        return len(self.labels)

train_dataset = IMDbDataset(train_encodings, train_labels)
val_dataset = IMDbDataset(val_encodings, val_labels)
test_dataset = IMDbDataset(test_encodings, test_labels)

device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')

def model_func():
    return BertForSequenceClassification.from_pretrained('bert-base-uncased')

lr = 5e-5
betas = (0.9, 0.999)
eps = 1e-6
weight_decay = 0

config = {
    # The same format as optimizer config of DeepSpeed
    # https://www.deepspeed.ai/docs/config-json/#optimizer-parameters
    "optimizer": {
        "type": "Adam",
        "params": {
            "lr": lr,
            "betas": betas,
            "eps": eps,
            "weight_decay": weight_decay,
            "use_hybrid_adam": True
        }
    },
    "fp16": {
        "enabled": True,
        "loss_scale": 0,
        "initial_scale_power": 2**3,
        "loss_scale_window": 1000,
        "hysteresis": 2,
        "min_loss_scale": 1
    },
    "default_chunk_size": 64 * 1024 * 1024,
    "use_fake_dist": False,
    "use_cpu_embedding": False
}

model, optim = initialize_engine(model_func=model_func,
                                      local_rank=0,
                                      config=config)

train_loader = DataLoader(train_dataset, batch_size=16, shuffle=True)

for epoch in range(3):
    for i, batch in enumerate(train_loader):
        optim.zero_grad()
        input_ids = batch['input_ids'].to(device)
        #print(input_ids)
        attention_mask = batch['attention_mask'].to(device)
        labels = batch['labels'].to(device)
        outputs = model(input_ids, attention_mask=attention_mask, labels=labels)
        loss = outputs[0]
        model.backward(loss)
        optim.step()
        print(i, loss.item())
        if i == 10:
            exit()

model.eval()