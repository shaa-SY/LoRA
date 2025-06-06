import json
import pandas as pd
import torch
from datasets import Dataset
from modelscope import AutoTokenizer
from swanlab.integration.huggingface import SwanLabCallback
from peft import LoraConfig, TaskType, get_peft_model
from transformers import AutoModelForCausalLM, TrainingArguments, Trainer, DataCollatorForSeq2Seq
import os
import swanlab

# 权重根目录
BASE_DIR = 'D:\ModelSpace\Qwen2'

# 设备名称
device = 'cuda' if torch.cuda.is_available() else 'cpu'
# device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

# 数据集处理函数，包括：训练数据集和测试数据集
def dataset_jsonl_transfer(origin_path, new_path):
    """
    将原始数据集转换为大模型微调所需数据格式的新数据集
    """
    messages = []

    # 读取原JSONL文件
    with open(origin_path, "r", encoding="utf-8") as file:
        for line in file:
            # 解析每一行原始数据（每一行均是一个JSON格式）
            data = json.loads(line)
            text = data["text"]
            catagory = data["category"]
            output = data["output"]
            message = {
                "input": f"文本:{text},分类选项列表:{catagory}",
                "output": output,
            }
            messages.append(message)

    # 保存处理后的JSONL文件，每行也是一个JSON格式
    with open(new_path, "w", encoding="utf-8") as file:
        for message in messages:
            file.write(json.dumps(message, ensure_ascii=False) + "\n")

# 在使用数据集训练大模型之前，对每行数据进行预处理
def process_func(example):
    """
    将数据集进行预处理
    """
    MAX_LENGTH = 384
    input_ids, attention_mask, labels = [], [], []
    instruction = tokenizer(f"<|im_start|>system\n你是一个文本分类领域的专家，你会接收到一段文本和几个潜在的分类选项列表，请输出文本内容的正确分类<|im_end|>\n<|im_start|>user\n{example['input']}<|im_end|>\n<|im_start|>assistant\n", add_special_tokens=False)  # add_special_tokens 不在开头加 special_tokens
    response = tokenizer(f"{example['output']}", add_special_tokens=False)
    input_ids = instruction["input_ids"] + response["input_ids"] + [tokenizer.pad_token_id]
    attention_mask = instruction["attention_mask"] + response["attention_mask"] + [1]  # 因为eos token咱们也是要关注的所以 补充为1
    labels = [-100] * len(instruction["input_ids"]) + response["input_ids"] + [tokenizer.pad_token_id]

    if len(input_ids) > MAX_LENGTH:  # 做一个截断
        input_ids = input_ids[:MAX_LENGTH]
        attention_mask = attention_mask[:MAX_LENGTH]
        labels = labels[:MAX_LENGTH]

    return {
        "input_ids": input_ids,
        "attention_mask": attention_mask,
        "labels": labels
    }

# 加载预训练模型和分词器
model_dir = os.path.join(BASE_DIR, 'Qwen2-0.5B')
tokenizer = AutoTokenizer.from_pretrained(model_dir, use_fast=False, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_dir, device_map=device, torch_dtype=torch.bfloat16)
model.enable_input_require_grads() # 开启梯度检查点时，要执行该方法

# 加载、处理数据集和测试集
train_dataset_path = os.path.join(BASE_DIR, 'zh_cls_fudan-news', 'train.jsonl')
test_dataset_path = os.path.join(BASE_DIR, 'zh_cls_fudan-news', 'test.jsonl')

train_jsonl_new_path = os.path.join(BASE_DIR, 'train.jsonl')
test_jsonl_new_path = os.path.join(BASE_DIR, 'test.jsonl')

if not os.path.exists(train_jsonl_new_path):
    dataset_jsonl_transfer(train_dataset_path, train_jsonl_new_path)
if not os.path.exists(test_jsonl_new_path):
    dataset_jsonl_transfer(test_dataset_path, test_jsonl_new_path)

# 得到微调数据集
train_df = pd.read_json(train_jsonl_new_path, lines=True)
train_ds = Dataset.from_pandas(train_df)
train_dataset = train_ds.map(process_func, remove_columns=train_ds.column_names)

# 创建LoRA配置
config = LoraConfig(
    task_type=TaskType.CAUSAL_LM,
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    inference_mode=False,  # 训练模式
    r=8,  # Lora 秩
    lora_alpha=32,  # Lora alaph，具体作用参见 Lora 原理
    lora_dropout=0.1,  # Dropout 比例
)

# 将LoRA应用于模型
model = get_peft_model(model, config)

# 创建微调参数
args = TrainingArguments(
    output_dir=os.path.join(BASE_DIR, 'output', 'Qwen2-0.5B'),
    per_device_train_batch_size=4,
    gradient_accumulation_steps=4,
    logging_steps=10,
    num_train_epochs=2,
    save_steps=100,
    learning_rate=1e-4,
    save_on_each_node=True,
    gradient_checkpointing=True,
    report_to="none",
)

# SwanLab微调过程回调数据
swanlab_callback = SwanLabCallback(project="Qwen2-FineTuning", experiment_name="Qwen2-0.5B")

trainer = Trainer(
    model=model,
    args=args,
    train_dataset=train_dataset,
    data_collator=DataCollatorForSeq2Seq(tokenizer=tokenizer, padding=True),
    callbacks=[swanlab_callback],
)

# 开始微调
trainer.train()

# 模型结果结果评估
def predict(messages, model, tokenizer):
    text = tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True
    )
    model_inputs = tokenizer([text], return_tensors="pt").to(device)

    generated_ids = model.generate(
        model_inputs.input_ids,
        max_new_tokens=512
    )
    generated_ids = [
        output_ids[len(input_ids):] for input_ids, output_ids in zip(model_inputs.input_ids, generated_ids)
    ]

    return tokenizer.batch_decode(generated_ids, skip_special_tokens=True)[0]

# 模型评估：获取测试集的前10条测试数据
test_df = pd.read_json(test_jsonl_new_path, lines=True)[:10]

test_text_list = []
for index, row in test_df.iterrows():
    instruction = row['你是一个文本分类领域的专家，你会接收到一段文本和几个潜在的分类选项列表，请输出文本内容的正确分类']
    input_value = row['input']

    messages = [
        {"role": "system", "content": f"{instruction}"},
        {"role": "user", "content": f"{input_value}"}
    ]

    response = predict(messages, model, tokenizer)
    messages.append({"role": "assistant", "content": f"{response}"})

    result_text = f"{messages[0]}\n\n{messages[1]}\n\n{messages[2]}"
    test_text_list.append(swanlab.Text(result_text, caption=response))

swanlab.log({"Prediction": test_text_list})
swanlab.finish()