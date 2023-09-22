import pandas as pd
import random, os, json, multiprocessing
from tqdm import tqdm
from .utils import _f, Utils
from sentence_transformers import SentenceTransformer
import numpy as np
import faiss
import platform
from .ize import charge
def _create_index(embeddings, use_gpu):
    index = faiss.IndexFlatIP(len(embeddings[0]))
    embeddings = np.asarray(embeddings, dtype=np.float32)
    if use_gpu:
        co = faiss.GpuMultipleClonerOptions()
        co.shard = True
        co.useFloat16 = True
        index = faiss.index_cpu_to_all_gpus(index, co=co)
    index.add(embeddings)
    return index

def _batch_search(index, query, topk, batch_size: int = 64):
    all_scores, all_inxs = [], []
    for start_index in tqdm(
        range(0, len(query), batch_size),
        desc=_f("wait", "searching", no_print=True),
        disable=len(query) < 256,
    ):
        batch_query = query[start_index : start_index + batch_size]
        batch_scores, batch_inxs = index.search(
            np.asarray(batch_query, dtype=np.float32), k=topk
        )
        all_scores.extend(batch_scores.tolist())
        all_inxs.extend(batch_inxs.tolist())
    return all_scores, all_inxs

def _score_data_job(args):
    (
        group_by,
        chunk_start,
        chunk_end,
        df,
        training_data,
        model,
        prompt,
        task
    ) = args
    try:
        pbar = tqdm(range(chunk_start, chunk_end))
        for i in pbar:
            context_index, sentences_index = random.randint(0, len(df)), random.randint(
                0, len(df)
            )
            q1, q2 = df["sentences"].iloc[sentences_index], \
                        df["sentences"].iloc[context_index]
            _min = min([len(q1), len(q2)])
            if _min > 2:
                if task == 'similarity':
                    q1, q2 = (random.sample(list(q1), _min), random.sample(list(q2), _min))
                elif task == 'retrieval':
                    q1, q2 = ([prompt + s for s in random.sample(list(q1), 1)], list(q2))
                emb1 = model.encode(q1, normalize_embeddings=True)
                emb2 = model.encode(q2, normalize_embeddings=True)
                scores = emb1 @ emb2.T
                for _q, _score in zip(q1, scores):
                    _df = pd.DataFrame(
                            [
                                {
                                    "sentences": _q,
                                    "id": int(df[group_by].iloc[sentences_index]),
                                    "scores": _score,
                                    "context_sentences": q2,
                                    "context_id": df[group_by].iloc[context_index],
                                }
                            ]
                        )
                    training_data = pd.concat([training_data, _df], ignore_index=True)
                    pbar.set_description(
                        _f(
                            "success",
                            f"sample {i} - comparing {int(df[group_by].iloc[sentences_index])} 🧮 {df[group_by].iloc[context_index]}",
                            no_print=True,
                        ),
                        refresh=True,
                    )
    except Exception as e:
        _f('fatal', e)
        raise Exception
    return training_data

class FinePrep:
    def __init__(self):
        self.df = None
        self.utils = Utils()

    def load(self, raw: str | pd.DataFrame = None):
        try:
            if isinstance(raw, str):
                raw_data_dir = os.path.join(raw)
                file_extension = os.path.splitext(raw)[-1]
                file_handlers = {
                    ".csv": pd.read_csv,
                    ".json": pd.read_json,
                    ".xlsx": pd.read_excel,
                    ".parquet": pd.read_parquet,
                }
                if file_extension in file_handlers:
                    self.df = file_handlers[file_extension](raw_data_dir)
                    _f("success", f"loaded - {raw_data_dir}")
                else:
                    _f("fatal", "unsupported file type")
            elif isinstance(raw, pd.DataFrame):
                self.df = raw
                _f("success", f"loaded - {raw}")
            else:
                _f("fatal", "data type not in [csv, json, xlsx, parquet, pd.DataFrame]")
        except Exception as e:
            _f("fatal", e)

    def save(self, filename: str = None, raw: pd.DataFrame = None):
        try:
            file_extension = os.path.splitext(filename)[-1]
            file_handlers = {
                ".csv": raw.to_csv,
                ".json": raw.to_json,
                ".xlsx": raw.to_excel,
                ".parquet": raw.to_parquet,
            }
            if file_extension in file_handlers:
                file_handlers[file_extension](filename)
                _f("success", f"saved - {filename}")
            else:
                _f("fatal", "unsupported data")
        except Exception as e:
            _f("fatal", e)

    def generate_training_data(self
                               , out_dir: str = None
                               , split: int = 16
                               , k: int = 64
                               , index: str = None
                               , num_pos: int = 3
                               , num_neg: int = 7
                               , index_to_gpu: bool = False
                            ):
        data = self.df.sample(int(len(self.df)/split))
        f = open(os.path.join(out_dir,'finetune_kb_dataset.jsonl'), "w")
        pbar = tqdm(data.itertuples(), total=len(data))
        pole = charge.Pole()
        pole.load_embeddings(index, cuda = index_to_gpu)
        for row in pbar:
            kb_index = random.randint(0, len(data))
            q = data["sentences"].iloc[kb_index]
            pos_results = pole.search_document_embeddings(q, k=k, df=data)[0:num_pos]
            neg_results = pos_results[-1][0:num_neg]
            json.dump(
                {
                    "query": q,
                    "pos": [x for x in pos_results],
                    "neg": [x for x in neg_results],
                },
                f,
            )
            f.write("\n")
            pbar.set_description(
                _f(
                    "info",
                    f'processed  - "{row.sentences}"',
                    no_print=True,
                    luxe=True,
                ),
                refresh=True,
            )
        _f("success", f"written - {out_dir}")

    def generate_scored_data(
        self,
        group_by: str = "id",
        split: int = 16,
        model: str = "BAAI/bge-large-en-v1.5",
        use_multiprocessing: bool = False,
        prompt: str = None,
        task: str = None
    ):
        if task is None:
            return _f("fatal", 'please pass "retrieval" or "similarity" as `task`')
        if self.df is not None:
            try:
                _f(
                    "wait",
                    f"get coffee or tea - {int(len(self.df)/split)} (1/{split} of your data) processing...",
                )
                _model = SentenceTransformer(model)
                _prompt = (
                    "Generate a representation for this sentence that can be used to retrieve related articles："
                    if prompt is None and model == "BAAI/bge-large-en-v1.5"
                    else prompt
                )

                if use_multiprocessing:
                    num_processes = int(multiprocessing.cpu_count()/2) \
                        if platform.system() in ['Linux','Windows'] else multiprocessing.cpu_count()
                    chunk_size = int((int(len(self.df) / split) / num_processes))

                    with multiprocessing.Pool(processes=num_processes) as pool:
                        args_list = []
                        for i in range(num_processes):
                            training_data = pd.DataFrame()
                            (
                                training_data["sentences"],
                                training_data["id"],
                                training_data["scores"],
                                training_data["context_sentences"],
                                training_data["context_id"],
                            ) = ("", "", "", "", "")
                            chunk_start = i * chunk_size
                            chunk_end = (
                                (i + 1) * chunk_size
                                if i < num_processes - 1
                                else int(len(self.df) / split)
                            )
                            args_list.append(
                                (
                                    group_by,
                                    chunk_start,
                                    chunk_end,
                                    self.df,
                                    training_data,
                                    _model,
                                    _prompt,
                                    task,
                                )
                            )
                            _f(
                                "warn",
                                f"{i+1}/{num_processes} processes started from index {chunk_start} to {chunk_end}/{int(len(self.df) / split)} ({chunk_size})",
                            )

                        results = pool.map(_score_data_job, args_list)
                        training_data = pd.concat(results, ignore_index=True)
                else:
                    training_data = pd.DataFrame()
                    (
                        training_data["sentences"],
                        training_data["id"],
                        training_data["scores"],
                        training_data["context_sentences"],
                        training_data["context_id"],
                    ) = ("", "", "", "", "")
                    i = int((int(len(self.df) / split)))
                    args_list = [
                            group_by,
                            i,
                            i + 1,
                            self.df,
                            training_data,
                            _model,
                            _prompt,
                            task,
                        ]
                    for i in range(int(len(self.df) / split)):
                        _score_data_job(
                            args_list
                        )

                final_path = os.path.join(self.cleaned_dir, f"{self.filename}")
                self.save(final_path, training_data)
                self.df = training_data
            except Exception as e:
                _f("fatal", e)
        else:
            return _f("fatal", "no data loaded!")    


    def find_knn_neg(
        self,
        model: str = "BAAI/bge-large-en-v1.5",
        input_file: str = None,
        output_file: str = None,
        sample_range: list | str = [0 - 200],
        num_hard_negatives: int = 15,
        use_gpu: bool = False,
        prompt=None,
    ):
        try:
            _model = SentenceTransformer(model)
            corpus = []
            queries = []
            train_data = []
            for line in open(
                os.path.join(os.path.abspath(self.cleaned_dir), input_file)
            ):
                line = json.loads(line.strip())
                train_data.append(line)
                corpus.extend(line["neg"])
                queries.append(line["query"])

            corpus = list(set(corpus))

            _f(
                "wait",
                f"inferencing massive embedding for corpus index - {len(corpus)}",
            )
            p_vecs = _model.encode(corpus, batch_size=256)
            _f(
                "wait",
                f"inferencing massive embedding for search queries - {len(queries)}",
            )
            prompt = (
                "Generate a representation for this sentence that can be used to retrieve related articles："
                if prompt == None
                else prompt
            )
            q_vecs = _model.encode([prompt + q for q in queries], batch_size=256)

            _f("success", "create index and search")
            index = _create_index(p_vecs, use_gpu=use_gpu)
            _, all_inxs = _batch_search(index, q_vecs, topk=sample_range[-1])
            assert len(all_inxs) == len(train_data)

            for i, data in enumerate(train_data):
                query = data["query"]
                inxs = all_inxs[i][sample_range[0] : sample_range[1]]
                filtered_inx = []
                for inx in inxs:
                    if inx == -1:
                        break
                    if corpus[inx] not in data["pos"] and corpus[inx] != query:
                        filtered_inx.append(inx)

                if len(filtered_inx) > num_hard_negatives:
                    filtered_inx = random.sample(filtered_inx, num_hard_negatives)
                data["neg"] = [corpus[inx] for inx in filtered_inx]
            final_path = os.path.join(
                os.path.abspath(self.cleaned_dir), f"{output_file}.jsonl"
            )
            with open(final_path, "w") as f:
                for data in train_data:
                    if len(data["neg"]) < num_hard_negatives:
                        data["neg"].extend(
                            random.sample(corpus, num_hard_negatives - len(data["neg"]))
                        )
                    f.write(json.dumps(data) + "\n")
            return _f("success", f"written - {final_path}")
        except Exception as e:
            _f("fatal", e)