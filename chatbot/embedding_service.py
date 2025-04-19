import os
import asyncio
import torch
import numpy as np
from transformers import AutoTokenizer, AutoModel
from huggingface_hub import snapshot_download
from quart import current_app

class ArabertEmbeddingService:
    def __init__(self):
        self.model_name = "aubmindlab/bert-base-arabert"
        self.local_path = "./models/bert-base-arabert"
        self.tokenizer = None
        self.model = None
        self.max_length = 512
        self.device = "cuda" if torch.cuda.is_available() else "cpu"

    async def initialize(self):
        if not os.path.exists(self.local_path):
            current_app.logger.info(
                f"Downloading model {self.model_name} to {self.local_path}..."
            )
            snapshot_download(
                repo_id=self.model_name,
                local_dir=self.local_path,
                resume_download=True
            )
        self.tokenizer = AutoTokenizer.from_pretrained(self.local_path)
        self.model = AutoModel.from_pretrained(self.local_path).to(self.device)
        self.model.eval()
        current_app.logger.info(
            f"Model {self.model_name} initialized on {self.device}"
        )

    async def get_embedding(self, text: str) -> np.ndarray:
        if not text or not text.strip():
            return np.zeros(768, dtype=np.float32)
        encoded = self.tokenizer(
            text,
            padding=True,
            truncation=True,
            max_length=self.max_length,
            return_tensors="pt"
        ).to(self.device)
        with torch.no_grad():
            out = self.model(**encoded)
        emb = self.mean_pooling(out, encoded["attention_mask"])
        return emb[0].cpu().numpy()

    def mean_pooling(self, model_output, attention_mask):
        token_emb = model_output.last_hidden_state
        mask = attention_mask.unsqueeze(-1).expand(token_emb.size()).float()
        summed = torch.sum(token_emb * mask, dim=1)
        counts = torch.clamp(mask.sum(dim=1), min=1e-9)
        return summed / counts

    async def get_similarity(self, t1: str, t2: str) -> float:
        e1, e2 = await asyncio.gather(
            self.get_embedding(t1),
            self.get_embedding(t2)
        )
        norm = np.linalg.norm(e1) * np.linalg.norm(e2)
        return float(np.dot(e1, e2) / norm) if norm > 0 else 0.0
