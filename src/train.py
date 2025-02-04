import torch
import torch.nn as nn
import torch.optim as optim
from tqdm import tqdm
import os
import sys

# Add the src directory to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.dataset import EmotionDataset
from src.model import EmotionRecognitionModel
from src.utils import Logger, save_checkpoint

def train(config):
    # Initialize logger
    logger = Logger(config['log_dir'])
    
    # Set device and print GPU info if available
    cuda_available = torch.cuda.is_available()
    device = torch.device('cuda' if cuda_available else 'cpu')
    
    if cuda_available:
        logger.log(f'Using GPU: {torch.cuda.get_device_name(0)}')
        logger.log(f'GPU Memory: {torch.cuda.get_device_properties(0).total_memory / 1024**3:.2f} GB')
    else:
        logger.log('CUDA is not available. Using CPU for training (this will be slower)')
    
    # Create datasets and dataloaders
    try:
        train_dataset = EmotionDataset(
            data_dir=os.path.join(config['data_dir'], 'train'),
            mode='train'
        )
        val_dataset = EmotionDataset(
            data_dir=os.path.join(config['data_dir'], 'val'),
            mode='val'
        )
    except Exception as e:
        logger.log(f"Error creating datasets: {str(e)}")
        return
    
    # Configure DataLoader parameters
    dataloader_kwargs = {
        'batch_size': config['batch_size'],
        'num_workers': 2 if os.name == 'nt' else min(os.cpu_count(), 4),  # 0 for Windows
        'pin_memory': cuda_available
    }
    
    train_loader = train_dataset.get_dataloader(
        shuffle=True,
        **dataloader_kwargs
    )
    val_loader = val_dataset.get_dataloader(
        shuffle=False,
        **dataloader_kwargs
    )
    
    logger.log(f'Number of classes: {train_dataset.get_num_classes()}')
    logger.log(f'Training samples: {len(train_dataset.dataset)}')
    logger.log(f'Validation samples: {len(val_dataset.dataset)}')
    
    # Initialize model
    num_classes = train_dataset.get_num_classes()
    model = EmotionRecognitionModel(num_classes=num_classes)
    model = model.to(device)
    
    # Initialize scaler for mixed precision training if CUDA is available
    scaler = torch.amp.GradScaler(enabled=cuda_available) # Pass cuda_available instead
    
    # Loss function and optimizer
    criterion = nn.CrossEntropyLoss()
    optimizer = optim.Adam(
        model.parameters(),
        lr=config['learning_rate'],
        weight_decay=config['weight_decay']
    )
    
    # Learning rate scheduler
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, mode='max', factor=0.1, patience=5
    )
    
    # Training loop
    best_val_acc = 0.0
    logger.log("Starting training...")
    
    for epoch in range(config['num_epochs']):
        model.train()
        train_loss = 0.0
        correct = 0
        total = 0
        
        # Training phase
        pbar = tqdm(train_loader, desc=f'Epoch {epoch+1}/{config["num_epochs"]}')
        for inputs, labels in pbar:
            inputs, labels = inputs.to(device), labels.to(device)
            
            optimizer.zero_grad()
            
            if cuda_available:
                # Use automatic mixed precision
                with torch.amp.autocast(device_type='cuda', dtype=torch.float16):  # Correct usage
                    outputs = model(inputs)
                    loss = criterion(outputs, labels)
                
                scaler.scale(loss).backward()
                scaler.step(optimizer)
                scaler.update()
            else:
                # Regular training
                outputs = model(inputs)
                loss = criterion(outputs, labels)
                loss.backward()
                optimizer.step()
            
            train_loss += loss.item()
            _, predicted = outputs.max(1)
            total += labels.size(0)
            correct += predicted.eq(labels).sum().item()
            
            pbar.set_postfix({
                'loss': train_loss/total, 
                'acc': 100.*correct/total,
                'lr': optimizer.param_groups[0]['lr']
            })
        
        # Validation phase
        model.eval()
        val_loss = 0.0
        val_correct = 0
        val_total = 0
        
        with torch.no_grad():
            for inputs, labels in val_loader:
                inputs, labels = inputs.to(device), labels.to(device)
                
                if cuda_available:
                    with torch.cuda.amp.autocast():
                        outputs = model(inputs)
                        loss = criterion(outputs, labels)
                else:
                    outputs = model(inputs)
                    loss = criterion(outputs, labels)
                
                val_loss += loss.item()
                _, predicted = outputs.max(1)
                val_total += labels.size(0)
                val_correct += predicted.eq(labels).sum().item()
        
        val_acc = 100. * val_correct / val_total
        
        # Update learning rate scheduler
        scheduler.step(val_acc)
        current_lr = optimizer.param_groups[0]['lr']  # Use this for logging LR
        logger.log(f"Current Learning rate is {current_lr}")
        
        # Log metrics
        logger.log(f'Epoch {epoch+1}/{config["num_epochs"]}:')
        logger.log(f'Train Loss: {train_loss/len(train_loader):.4f}, '
                  f'Train Acc: {100.*correct/total:.2f}%')
        logger.log(f'Val Loss: {val_loss/len(val_loader):.4f}, '
                  f'Val Acc: {val_acc:.2f}%')
        
        # Save checkpoint if validation accuracy improves
        if val_acc > best_val_acc:
            best_val_acc = val_acc
            save_checkpoint(
                model, optimizer, epoch,
                val_loss/len(val_loader), val_acc,
                config['checkpoint_dir']
            )
            logger.log(f'New best model saved with validation accuracy: {val_acc:.2f}%')

if __name__ == '__main__':
    config = {
        'data_dir': '../data/split_images',
        'log_dir': '../logs',
        'checkpoint_dir': '../checkpoints',
        'batch_size': 64,  # Reduced batch size for initial testing
        'learning_rate': 1e-4,
        'weight_decay': 1e-5,
        'num_epochs': 50,
    }
    
    # Create necessary directories
    os.makedirs(config['log_dir'], exist_ok=True)
    os.makedirs(config['checkpoint_dir'], exist_ok=True)
    
    # Set random seeds for reproducibility
    torch.manual_seed(42)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(42)
    
    train(config)