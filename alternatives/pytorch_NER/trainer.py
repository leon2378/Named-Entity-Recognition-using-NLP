import time

import numpy as np
import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader
from sklearn.metrics import accuracy_score, f1_score, classification_report
from sklearn.metrics import classification_report
from sklearn.metrics import confusion_matrix
import matplotlib.pyplot as plt
import seaborn as sns
from tqdm import tqdm 


from dataloader import CoNLLDataset
from classifier import NERClassifier
from utils import save_checkpoint, log_gradient_norm


def evaluate_model(model, dataloader, writer, device, mode, step, class_mapping=None):
    """Evaluates the model performance."""
    if mode not in ["Train", "Validation", "Test"]:
        raise ValueError(
            f"Invalid value for mode! Expected 'Train', 'Validation' or 'Test' but received {mode}"
        )

    if class_mapping is None:
        raise ValueError("Argument @class_mapping not provided!")

    y_true_accumulator = []
    y_pred_accumulator = []

    #print("Started model evaluation.")
    for x, y, padding_mask in tqdm(dataloader, desc=f"Eval {mode}"):
        x, y = x.to(device), y.to(device)
        padding_mask = padding_mask.to(device)
        y_pred = model(x, padding_mask)

        # Extract predictions and labels only for pre-padding tokens
        unpadded_mask = torch.logical_not(padding_mask)
        y_pred = y_pred[unpadded_mask]
        y = y[unpadded_mask]

        y_pred = y_pred.argmax(dim=1)
        y_pred = y_pred.view(-1).detach().cpu().tolist()
        y = y.view(-1).detach().cpu().tolist()

        y_true_accumulator += y
        y_pred_accumulator += y_pred

    # Map the integer labels back to NER tags
    y_pred_accumulator = [class_mapping[str(pred)] for pred in y_pred_accumulator]
    y_true_accumulator = [class_mapping[str(pred)] for pred in y_true_accumulator]

    y_pred_accumulator = np.array(y_pred_accumulator)
    y_true_accumulator = np.array(y_true_accumulator)

    # Extract labels and predictions where target label isn't O
    non_O_ind = np.where(y_true_accumulator != "O")
    y_pred_non_0 = y_pred_accumulator[non_O_ind]
    y_true_non_0 = y_true_accumulator[non_O_ind]

    # Calculate and log accuracy
    accuracy_total = accuracy_score(y_true_accumulator, 
                                    y_pred_accumulator)
    accuracy_non_O = accuracy_score(y_true_non_0,
                                    y_pred_non_0)
    writer.add_scalar(f"{mode}/Accuracy-Total",
                      accuracy_total, step)
    writer.add_scalar(f"{mode}/Accuracy-Non-O",
                      accuracy_non_O, step)

    # Calculate and log F1 score
    f1_total = f1_score(y_true_accumulator,
                        y_pred_accumulator,
                        average="weighted")
    f1_non_O = f1_score(y_true_non_0,
                        y_pred_non_0,
                        average="weighted")
    writer.add_scalar(f"{mode}/F1-Total",
                      f1_total, step)
    writer.add_scalar(f"{mode}/F1-Non-O",
                      f1_non_O, step)

    label_names = class_mapping.values()
    #print(f"label_names={label_names}")
    if mode=="Test":
        cm = confusion_matrix(y_true_accumulator, y_pred_accumulator)
        plt.figure(figsize=(10,7))
        sns.heatmap(cm, annot=True, fmt='d', cmap='Blues', xticklabels=label_names, yticklabels=label_names)
        plt.xlabel('Predicted')
        plt.ylabel('True')
        plt.title('Confusion Matrix')
        plt.savefig('../../images/pytorch_NER_confusion_matrix.png')  # Save as PNG
        plt.close()
    else:
        print(classification_report(y_true_accumulator, y_pred_accumulator, digits=4, zero_division=0))


def train_loop(config, writer, device):
    """Implements training of the model.

    Arguments:
        config (dict): Contains configuration of the pipeline
        writer: tensorboardX writer object
        device: device on which to map the model and data
    """
    torch.manual_seed(config["seed"])
    np.random.seed(config["seed"])
    reverse_class_mapping = {
        str(idx): cls_name for cls_name, idx in config["class_mapping"].items()
    }
    # Define dataloader hyper-parameters
    train_hyperparams = {
        "batch_size": config["batch_size"]["train"],
        "shuffle": True,
        "drop_last": True
    }
    valid_hyperparams = {
        "batch_size": config["batch_size"]["validation"],
        "shuffle": False,
        "drop_last": True
    }

    # Create dataloaders
    train_set = CoNLLDataset(config, config["dataset_path"]["train"])
    valid_set = CoNLLDataset(config, config["dataset_path"]["validation"])
    test_set = CoNLLDataset(config, config["dataset_path"]["test"])
    train_loader = DataLoader(train_set, **train_hyperparams)
    valid_loader = DataLoader(valid_set, **valid_hyperparams)
    test_loader = DataLoader(test_set, **valid_hyperparams)

    # Instantiate the model
    model = NERClassifier(config)
    print(model)
    model = model.to(device)

    # Load training configuration
    train_config = config["train_config"]
    learning_rate = train_config["learning_rate"]

    # Prepare the model optimizer
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=train_config["learning_rate"],
        weight_decay=train_config["l2_penalty"]
    )

    # Weights used for Cross-Entropy loss
    # Calculated as log(1 / (class_count / train_samples))
    # @class_count: Number of tokens in the corpus per each class
    # @train_samples:  Total number of samples in the trains set
    class_w = train_config["class_w"]
    class_w = torch.tensor(class_w).to(device)
    class_w /= class_w.sum()

    train_step = 0
    start_time = time.strftime("%b-%d_%H-%M-%S")
    
    epoch_num = train_config["num_of_epochs"] 

    # for debug
    #epoch_num = 0

    for epoch in range(epoch_num):
        #print("Epoch:", epoch)
        model.train()

        for x, y, padding_mask in tqdm(train_loader, desc=f"Train Epoch {epoch}"):
            train_step += 1
            x, y = x.to(device), y.to(device)
            padding_mask = padding_mask.to(device)

            optimizer.zero_grad()
            y_pred = model(x, padding_mask)

            # Extract predictions and labels only for pre-padding tokens
            unpadded_mask = torch.logical_not(padding_mask)
            y = y[unpadded_mask]
            y_pred = y_pred[unpadded_mask]

            loss = F.cross_entropy(y_pred, y, weight=class_w)

            # Update model weights
            loss.backward()

            log_gradient_norm(model, writer, train_step, "Before")
            torch.nn.utils.clip_grad_norm_(model.parameters(), train_config["gradient_clipping"])
            log_gradient_norm(model, writer, train_step, "Clipped")
            optimizer.step()

            writer.add_scalar("Train/Step-Loss", loss.item(), train_step)
            writer.add_scalar("Train/Learning-Rate", learning_rate, train_step)

        with torch.no_grad():
            model.eval()
            evaluate_model(model, train_loader, writer, device,
                           "Train", epoch, reverse_class_mapping)
            evaluate_model(model, valid_loader, writer, device,
                           "Validation", epoch, reverse_class_mapping)
            model.train()

        #save_checkpoint(model, start_time, epoch)
        print()

    evaluate_model(model, test_loader, writer, device,
                           "Test", epoch_num, reverse_class_mapping)
