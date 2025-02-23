import os
os.environ["CUDA_VISIBLE_DEVICES"]="0"

from math import ceil
import numpy as np
import pandas as pd
from pytorch_transformers import RobertaConfig, RobertaTokenizer, RobertaModel

from sklearn.preprocessing import MinMaxScaler
import sklearn.metrics as mt
from sklearn.model_selection import KFold
import torch
import torch.nn as nn
from torch.optim import Adam
from torch.utils.data import DataLoader

from model import MatchArchitecture
from data_utils import MatchingDataset

RANDOM_SEED = 117
SEQ_LEN = 10
RNN_DIM = 64
LINEAR_DIM = 64
CLASSES = 1
ROBERTA_FEAT_SIZE = 768
ADDITIONAL_FEAT_SIZE = 0
F1_POS_THRESHHOLD = .3
epsilon = 1e-8

tokenizer = RobertaTokenizer.from_pretrained('roberta-base')

train_size = 8870
oof_preds = np.zeros((train_size, 1))
oof_preds2 = np.zeros((train_size, 1))
oof_labels = np.zeros((train_size, 1))
cur_oof_inx = 0
for fold in range(5):
    VERSION = '1.1_fold_{}'.format(fold)
    SAVE_DIR = '/ssd-1/clinical/clinical-abbreviations/checkpoints/{}.pt'.format(VERSION)
    train_data_path = '/ssd-1/clinical/clinical-abbreviations/code/data/Train1_train.csv'
    val_data_path = '/ssd-1/clinical/clinical-abbreviations/code/data/Train1_val.csv'
    features_path = '/ssd-1/clinical/clinical-abbreviations/data/full_train.csv'

    load_data = True
    if load_data:
        path = '/ssd-1/clinical/clinical-abbreviations/code/data/'
        positives = pd.read_csv(path + 'Train1.csv', sep='|')
        negatives = pd.read_csv(path + 'Train2.csv', sep='|')

        train_strings = pd.concat((positives, negatives), axis=0)
        additional_feats = pd.read_csv(features_path)
        additional_feats.drop("Unnamed: 0", axis=1, inplace=True)
        if "target" in additional_feats.columns:
            additional_feats.drop("target", axis=1, inplace=True)
        ADDITIONAL_FEAT_SIZE = additional_feats.shape[1]
        kf = KFold(n_splits=5, random_state = RANDOM_SEED, shuffle = True)


        # TODO:@Ray improve the fold selection
        cur_fold = 0
        for train_inx, val_inx in kf.split(train_strings):
            if cur_fold == fold:
                break
            cur_fold += 1

        X_train = train_strings.iloc[train_inx, :].reset_index(drop=True, inplace=False)
        X_feats = additional_feats.iloc[train_inx, :].reset_index(drop=True, inplace=False)
        X_test = train_strings.iloc[val_inx, :].reset_index(drop=True, inplace=False)
        X_feats_test = additional_feats.iloc[val_inx, :].reset_index(drop=True, inplace=False)

        X_feats = np.array(X_feats)
        X_feats_test = np.array(X_feats_test)
        scaler = MinMaxScaler()
        X_feats = scaler.fit_transform(X_feats)
        X_feats_test = scaler.fit_transform(X_feats_test)

        X_train.to_csv(train_data_path, index=False)
        X_test.to_csv(val_data_path, index=False)

    train_dataset = MatchingDataset(train_data_path, X_feats, tokenizer)
    val_dataset = MatchingDataset(val_data_path, X_feats_test, tokenizer)

    model = MatchArchitecture(
        None,
        'roberta-base',
        False,
        ROBERTA_FEAT_SIZE,
        ADDITIONAL_FEAT_SIZE,
        CLASSES,
        RNN_DIM,
        LINEAR_DIM,
    ).cuda()

    def lr_scheduler(epoch):
        if epoch < 5:
            return 1e-3
        if epoch < 8:
            return 1e-4
        else:
            return 1e-5

    train_config = {
        "batch_size": 16,
        "base_lr": .0001,
        "lr_shceduler": lr_scheduler,
        "n_epochs": 6
    }


    def _run_training_loop(model, train_config):
        """Runs the training loop to train the matcher."""
        # set up params for training loop

        criterion = nn.BCELoss(reduce=False)
        #criterion = torch.nn.MSELoss()

        opt = Adam(model.parameters(), lr=train_config["base_lr"])

        epoch_learn_rates = []
        epoch_train_losses = []
        epoch_train_f1s = []
        epoch_validation_losses = []
        epoch_validation_f1s = []
        train_steps_per_epoch = int(len(train_dataset) / train_config["batch_size"])
        validation_steps_per_epoch = int(len(val_dataset) / train_config["batch_size"])

        for epoch in range(train_config["n_epochs"]):

            train_generator = iter(DataLoader(train_dataset, batch_size=train_config["batch_size"], shuffle=True,
                                              num_workers=4))
            val_generator = iter(DataLoader(val_dataset, batch_size=train_config["batch_size"], shuffle=False,
                                            num_workers=4))

            adjusted_lr = lr_scheduler(epoch)
            for param_group in opt.param_groups:
                param_group["lr"] = adjusted_lr
                epoch_learn_rates.append(adjusted_lr)

            print("Epoch: {}. LR: {}.".format(epoch, adjusted_lr))


            model.train(True)
            running_train_loss = 0
            target_true = 0
            predicted_true = 0
            correct_true_preds = 0
            mask_sum = 0
            y_sum = 0
            for step in range(train_steps_per_epoch):
                # Calculate losses

                sample = next(train_generator)
                X_batch_1 = sample['text_1'].cuda()
                X_batch_2 = sample['text_2'].cuda()
                y_batch = sample['labels'].cuda()
                additional_feats = sample['additional_feats'].cuda()

                y_sum += torch.sum(y_batch).item() / train_config["batch_size"]
                model.zero_grad()
                sigmoid_output = model(X_batch_1, X_batch_2, additional_feats)

                loss = criterion(sigmoid_output, y_batch)
                loss = torch.mean(loss)

                y_batch = y_batch.cpu()

                # Calculate metrics
                running_train_loss += loss.cpu().item()

                threshold_output = (sigmoid_output > F1_POS_THRESHHOLD).cpu().type(torch.IntTensor)
                target_true += torch.sum(y_batch == 1).float().item()
                predicted_true += torch.sum(threshold_output).float().item()
                correct_true_preds += torch.sum(
                    ((threshold_output == y_batch) * threshold_output)
                    == 1).cpu().float().item()

                # Propogate
                loss.backward()
                opt.step()

                if step % 50 == 0:
                    print("train step: ", step, "loss: ", running_train_loss/(step + 1))
                    print("y_sum: ", y_sum/(step + 1))

                del loss, X_batch_1, X_batch_2, y_batch, sample, sigmoid_output, threshold_output

            recall = correct_true_preds / (target_true + .1)
            precision = correct_true_preds / (predicted_true +.1)
            epoch_train_f1 = 2 * (precision * recall) / (precision + recall + epsilon)
            epoch_train_f1s.append(epoch_train_f1)
            epoch_train_loss = running_train_loss / train_steps_per_epoch
            epoch_train_losses.append(epoch_train_loss)
            print("Epoch {}, train loss of {}.".format(epoch, epoch_train_loss))
            print("Epoch {}, train f1 of {}.".format(epoch, epoch_train_f1))

            model.train(False)
            running_validation_loss = 0
            val_target_true = 0
            val_predicted_true = 0
            val_correct_true_preds = 0
            for step in range(validation_steps_per_epoch):

                sample = next(val_generator)
                X_batch_1 = sample['text_1'].cuda()
                X_batch_2 = sample['text_2'].cuda()
                y_batch = sample['labels'].cuda()
                additional_feats = sample['additional_feats'].cuda()

                y_sum += torch.sum(y_batch).item() / train_config["batch_size"]
                model.zero_grad()
                sigmoid_output = model(X_batch_1, X_batch_2, additional_feats)

                loss = criterion(sigmoid_output, y_batch)
                loss = torch.mean(loss)

                y_batch = y_batch.cpu()
                # Calculate metrics
                running_validation_loss += loss.cpu().item()
                threshold_output = (sigmoid_output > F1_POS_THRESHHOLD).cpu().type(torch.IntTensor)
                val_target_true += torch.sum(y_batch == 1).float().item()
                val_predicted_true += torch.sum(threshold_output).float().item()
                val_correct_true_preds += torch.sum(
                    ((threshold_output == y_batch) * threshold_output)
                    == 1).cpu().float().item()


                del loss, X_batch_1, X_batch_2, y_batch, sample, sigmoid_output, threshold_output

            recall = val_correct_true_preds / (val_target_true +epsilon)
            precision = val_correct_true_preds / (val_predicted_true+epsilon)
            epoch_validation_f1 = 2 * (precision * recall) / (precision + recall + epsilon)
            epoch_validation_f1s.append(epoch_validation_f1)
            epoch_validation_loss = running_validation_loss / validation_steps_per_epoch
            epoch_validation_losses.append(epoch_validation_loss)
            print("Epoch {}, train loss of {}.".format(epoch, epoch_train_loss))
            print("Epoch {}, train f1 of {}.".format(epoch, epoch_train_f1))
            print("Epoch {}, validation loss of {}.".format(epoch, epoch_validation_loss))
            print("Epoch {}, validation f1 of {}.".format(epoch, epoch_validation_f1))


        torch.save(model.state_dict, SAVE_DIR + 'model.pt')

        train_history = {
            "f1": epoch_train_f1s,
            "loss": epoch_train_losses,
            "val_f1": epoch_validation_f1s,
            "val_loss": epoch_validation_losses,
            "lr": epoch_learn_rates,
        }
        return model, train_history

    model, train_hstory = _run_training_loop(model, train_config)

    model.train(False)
    validation_steps_per_epoch = ceil(len(val_dataset) / train_config["batch_size"])
    val_generator = iter(DataLoader(val_dataset, batch_size=train_config["batch_size"], shuffle=False,
                                    num_workers=4))
    preds = None
    for step in range(validation_steps_per_epoch):
        sample = next(val_generator)
        X_batch_1 = sample['text_1'].cuda()
        X_batch_2 = sample['text_2'].cuda()
        y_batch = sample['labels'].cuda()
        additional_feats = sample['additional_feats'].cuda()


        sigmoid_output = model(X_batch_1, X_batch_2, additional_feats)
        if preds is None:
            preds = sigmoid_output.cpu().detach().numpy()
            labels = y_batch.cpu().detach().numpy()
        else:
            temp_preds = sigmoid_output.cpu().detach().numpy()
            preds = np.concatenate([preds, temp_preds], axis=0)

    oof_preds[val_inx, 0] = preds[:len(val_inx), 0]
    cur_oof_inx += len(labels)
    del model

train_filename = '/ssd-1/clinical/clinical-abbreviations/data/full_train.csv'
train_dataframe = pd.read_csv(train_filename, na_filter=False).drop("Unnamed: 0", axis=1)
target = train_dataframe['target']
thresholds = [.3, .4, .5, .6, .7]
for threshold in thresholds:
    print('F1 at {}: '.format(threshold), mt.f1_score(target, oof_preds > threshold))
    print('Recall at {}: '.format(threshold), mt.recall_score(target, oof_preds > threshold))
    print('Precision at {}: '.format(threshold), mt.precision_score(target, oof_preds > threshold))


