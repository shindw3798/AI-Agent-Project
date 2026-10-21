"""정규화된 임베딩과 코사인 유사도로 런북을 검색한다."""
import hashlib
import json
import math
import os
import time
import uuid
from pathlib import Path
import httpx
from .applicability import applicable

ROOT = Path(__file__).resolve().parent.parent
MODEL = "text-embedding-3-small"
DIMENSIONS = 512
THRESHOLD = 0.31  # 개발용 검색 질문으로 정한 임계값. 비교 평가에서는 고정한다.
MARGIN = 0.03


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


class Embedder:
    def __init__(self, cache=None):
        self.cache = cache  # 평가에서는 쿼리 캐시를 사용하지 않는다.
        self.usage = []

    def embed(self, texts):
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("EMBEDDING_KEY_MISSING")
        response = httpx.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
            json={"model": MODEL, "input": texts, "dimensions": DIMENSIONS}, timeout=30)
        response.raise_for_status()
        data = response.json()
        self.usage.append({"model": data.get("model"), "usage": data.get("usage")})
        vectors = [x["embedding"] for x in sorted(data["data"], key=lambda x: x["index"])]
        if len(vectors) != len(texts) or any(len(v) != DIMENSIONS for v in vectors):
            raise ValueError("EMBEDDING_DIMENSION_MISMATCH")
        return [normalize(v) for v in vectors]


def normalize(vector):
    norm = math.sqrt(sum(v * v for v in vector))
    if not norm:
        raise ValueError("ZERO_VECTOR")
    return [v / norm for v in vector]


class RunbookIndex:
    def __init__(self, directory=None, index_path=None, embedder=None, threshold=THRESHOLD):
        self.directory = directory or ROOT / "data/runbooks"
        self.path = index_path or ROOT / "data/runbook_index.json"
        self.embedder = embedder or Embedder()
        self.threshold = threshold

    def source_hashes(self):
        return {p.name: digest(p.read_bytes()) for p in sorted(self.directory.glob("*.md"))}

    def build(self):
        started = time.perf_counter()
        docs = []
        for path in sorted(self.directory.glob("*.md")):
            text = path.read_text(encoding="utf-8").strip()
            # 짧은 런북의 번호별 절차를 나누지 않고 함께 보존한다.
            docs.append({"document_id": path.stem, "chunk_id": path.stem + ":0",
                         "title": text.splitlines()[0].lstrip("# "), "text": text,
                         "incident_type": path.stem, "source_version": digest(path.read_bytes())})
        vectors = self.embedder.embed([d["text"] for d in docs])
        result = {"model": MODEL, "dimensions": DIMENSIONS, "chunking": "whole-short-procedure-v1",
                  "source_hashes": self.source_hashes(), "documents": docs, "vectors": vectors,
                  "build_seconds": time.perf_counter() - started, "usage": self.embedder.usage}
        save(self.path, result)
        return result

    def load(self):
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if (data["source_hashes"] != self.source_hashes() or data["model"] != MODEL
                or data["dimensions"] != DIMENSIONS
                or data["chunking"] != "whole-short-procedure-v1"):
            raise ValueError("INDEX_MISMATCH")
        if len(data["documents"]) != len(data["vectors"]) or any(
                len(v) != DIMENSIONS for v in data["vectors"]):
            raise ValueError("INDEX_MISMATCH")
        return data

    def search(self, query, system_name, monitoring_summary, top_k=3):
        data = self.load()
        if monitoring_summary.get("status") == "healthy" and not monitoring_summary.get("alerts"):
            return {"status": "no_match", "evidence": [], "error_code": None}
        alerts = monitoring_summary.get("alerts", [])
        # 단어가 비슷해도 현재 알림에 적용되지 않는 문서는 후보에서 먼저 제외한다.
        eligible = [(doc, vector) for doc, vector in zip(data["documents"], data["vectors"])
                    if applicable(doc["document_id"], alerts)]
        if not eligible:
            return {"status": "no_match", "evidence": [], "error_code": None}
        # 자원 사용률만으로 장애를 단정하지 않고 활성 알림을 함께 확인한다.
        observed = "; ".join(alerts) or monitoring_summary.get("status", "")
        texts = [observed, query] if observed else [query]
        vectors = self.embedder.embed(texts)
        ranked = []
        for doc, vector in eligible:
            scores = [sum(a*b for a, b in zip(v, vector)) for v in vectors]
            score = scores[0] * .8 + scores[-1] * .2 if len(scores) == 2 else scores[0]
            ranked.append((score, doc))
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        best = ranked[0][0] if ranked else 0
        evidence = [
            dict(doc, system_name=system_name, score=round(score, 6),
                 evidence_id=f"{system_name}:{doc['chunk_id']}")
            for score, doc in ranked[:max(1, min(3, top_k))]
            if score >= self.threshold and score >= best - MARGIN]
        return {"status": "success" if evidence else "no_match", "evidence": evidence, "error_code": None}


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", encoding="utf-8-sig")
    result = RunbookIndex().build()
    print(json.dumps({k: result[k] for k in ("model", "dimensions", "build_seconds", "usage")}))
