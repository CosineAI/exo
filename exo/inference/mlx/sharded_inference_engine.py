import numpy as np
import mlx.core as mx
from ..inference_engine import InferenceEngine
from .sharded_model import StatefulShardedModel
from .sharded_utils import load_shard, get_image_from_str
from ..shard import Shard
from typing import Optional
from exo.download.shard_download import ShardDownloader
import asyncio
from concurrent.futures import ThreadPoolExecutor

class MLXDynamicShardInferenceEngine(InferenceEngine):
  def __init__(self, shard_downloader: ShardDownloader):
    self.shard = None
    self.shard_downloader = shard_downloader
    self.executor = ThreadPoolExecutor(max_workers=1)

  async def infer_prompt(self, request_id: str, shard: Shard, prompt: str, image_str: Optional[str] = None, inference_state: Optional[str] = None) -> (np.ndarray, str, bool):
    await self.ensure_shard(shard)
    loop = asyncio.get_running_loop()
    if image_str:
      image = await get_image_from_str(image_str)
      inputs = await loop.run_in_executor(self.executor, self.tokenizer, prompt, image, return_tensors="np")
      pixel_values = mx.array(inputs["pixel_values"])
      input_ids = mx.array(inputs["input_ids"])
      output_data = await loop.run_in_executor(self.executor, self.stateful_sharded_model.step, request_id, input_ids, pixel_values)
    else:
      input_ids = await loop.run_in_executor(self.executor, lambda: mx.array(self.tokenizer.encode(prompt)))
      output_data = await loop.run_in_executor(self.executor, self.stateful_sharded_model.step, request_id, input_ids)
    return np.array(output_data), "", output_data.size == 1 and output_data.item() == self.tokenizer.eos_token_id

  async def infer_tensor(self, request_id: str, shard: Shard, input_data: np.ndarray, inference_state: Optional[str] = None) -> (np.ndarray, str, bool):
    await self.ensure_shard(shard)
    input_tensor = mx.array(input_data)
    output_data = await asyncio.get_running_loop().run_in_executor(
      self.executor,
      self.stateful_sharded_model.step,
      request_id,
      input_tensor
    )
    return np.array(output_data), "", output_data.size == 1 and output_data.item() == self.tokenizer.eos_token_id

  async def ensure_shard(self, shard: Shard):
    if self.shard == shard:
      return

    model_path = await self.shard_downloader.ensure_shard(shard)

    if self.shard != shard:
      loop = asyncio.get_running_loop()

      # Run load_shard in a separate thread
      def load_shard_wrapper():
        return asyncio.run(load_shard(model_path, shard))

      model_shard, self.tokenizer = await loop.run_in_executor(
        self.executor,
        load_shard_wrapper
      )

      # Create StatefulShardedModel in the executor
      self.stateful_sharded_model = await loop.run_in_executor(
        self.executor,
        StatefulShardedModel,
        shard,
        model_shard
      )
      self.shard = shard
