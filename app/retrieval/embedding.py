"""Optional local E5 inference. Never downloads weights or sends input externally."""
from pathlib import Path


class LocalE5:
    model_id='intfloat/multilingual-e5-small'
    def __init__(self, directory):
        path=Path(directory)
        if not path.is_dir():
            raise ValueError('An existing local model directory is required')
        import torch
        from transformers import AutoModel, AutoTokenizer
        self.torch=torch
        self.tokenizer=AutoTokenizer.from_pretrained(str(path),local_files_only=True,trust_remote_code=False)
        self.model=AutoModel.from_pretrained(str(path),local_files_only=True,trust_remote_code=False).cpu().eval()

    def _encode(self, text):
        t=self.torch
        batch=self.tokenizer([text],max_length=512,padding=True,truncation=True,return_tensors='pt')
        with t.inference_mode():
            output=self.model(**batch).last_hidden_state
            mask=batch['attention_mask'][...,None]
            pooled=(output*mask).sum(1)/mask.sum(1)
            return t.nn.functional.normalize(pooled,p=2,dim=1)[0].tolist()

    def encode_query(self,text):
        return self._encode('query: '+text)

    def encode_passage(self,text):
        return self._encode('passage: '+text)
