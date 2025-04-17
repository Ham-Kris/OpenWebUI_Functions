"""
title: RagFlow DeepSeek CoT Optimizer
author: Kris Ham
author_email: krisham@mail.com
funding_url: https://www.medicnex.com/Alipay.png
description: >
    This Pipe optimizes the chain-of-thought (CoT) output of the DeepSeek-R1 RagFlow API on Open WebUI.
required_open_webui_version: 0.5.6
requirements:
version: 1.0.0
license: MIT
"""

import json
import httpx
import asyncio
import traceback
from typing import AsyncGenerator, Callable, Awaitable
from pydantic import BaseModel, Field


class Pipe:
    class Valves(BaseModel):
        API_BASE_URL: str = Field(
            default="https://api.siliconflow.com/v1",
            description="Base request URL for the DeepSeek API",
        )
        API_KEY: str = Field(
            default="", description="API key for authentication, retrievable from the console"
        )
        API_MODEL: str = Field(
            default="deepseek-reasoner",
            description="Name of the model for API requests, default 'deepseek-reasoner'. For multiple models, separate names with commas.",
        )

    def __init__(self):
        self.valves = self.Valves()
        self.data_prefix = "data:"
        self.emitter = None

    def pipes(self):
        models = self.valves.API_MODEL.split(",")
        return [{"id": m.strip(), "name": m.strip()} for m in models]

    async def pipe(
        self, body: dict, __event_emitter__: Callable[[dict], Awaitable[None]] = None
    ) -> AsyncGenerator[str, None]:
        """
        Streaming output with per-chunk post-processing. No longer splits on '\n' to avoid random line breaks.
        """
        self.emitter = __event_emitter__
        if not self.valves.API_KEY:
            yield json.dumps({"error": "API key not configured"}, ensure_ascii=False)
            return

        headers = {
            "Authorization": f"Bearer {self.valves.API_KEY}",
            "Content-Type": "application/json",
        }

        try:
            # Only take the last part of the model name
            model_id = body["model"].split(".", 1)[-1]
            payload = {**body, "model": model_id}
            messages = payload["messages"]

            # Correct consecutive identical roles
            for i in range(len(messages) - 1, 0, -1):
                if messages[i]["role"] == messages[i - 1]["role"]:
                    alt_role = "assistant" if messages[i]["role"] == "user" else "user"
                    messages.insert(
                        i, {"role": alt_role, "content": "[Unfinished thinking]"}
                    )

            # Assign a separate chunk_buffer and context for each request
            chunk_buffer = []  # Buffer for chunks that are not yet determined for processing
            context = {"first_think_found": False}

            async with httpx.AsyncClient(http2=True) as client:
                async with client.stream(
                    "POST",
                    f"{self.valves.API_BASE_URL}/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=300,
                ) as response:
                    if response.status_code != 200:
                        error = await response.aread()
                        yield self._format_error(response.status_code, error)
                        return

                    async for raw_line in response.aiter_lines():
                        if not raw_line.startswith(self.data_prefix):
                            continue

                        json_str = raw_line[len(self.data_prefix):].strip()
                        if json_str == "[DONE]":
                            # Final signal => force-process all remaining chunks and output
                            async for final_chunk in self._finalize_all_chunks(
                                chunk_buffer, context
                            ):
                                yield final_chunk
                            return

                        try:
                            data = json.loads(json_str)
                        except json.JSONDecodeError as e:
                            error_detail = f"Failed to parse JSON - content: {json_str}, reason: {e}"
                            yield self._format_error("JSONDecodeError", error_detail)
                            return

                        choices = data.get("choices", [])
                        if not choices:
                            continue

                        choice = choices[0]
                        if choice.get("finish_reason"):
                            # Also force output remaining chunks
                            async for final_chunk in self._finalize_all_chunks(
                                chunk_buffer, context
                            ):
                                yield final_chunk
                            return

                        reasoning_part = choice["delta"].get("reasoning_content", "")
                        content_part = choice["delta"].get("content", "")
                        new_chunk = reasoning_part + content_part

                        # Append the new chunk to the buffer
                        chunk_buffer.append(new_chunk)

                        # Try to process chunks that can be finalized
                        finalized = self._try_finalize_chunks(
                            chunk_buffer, context, partial=True
                        )
                        for fc in finalized:
                            # Note: no extra newline appended at the end here
                            yield fc

        except Exception as e:
            # On error, also force output all remaining buffered chunks
            async for fc in self._finalize_all_chunks(chunk_buffer, context):
                yield fc
            yield self._format_exception(e)

    def _try_finalize_chunks(self, buffer, context, partial=True):
        """
        When partial=True (stream in progress):
         - We only process "determinable" chunks (especially checking if '</think>' is the last occurrence).
         - If there are fewer than 5 subsequent chunks to confirm, leave it in the buffer until more data arrives.
        Returns a list of processed chunks, which are removed from the buffer; undetermined chunks remain in the buffer.
        """
        finalized = []
        i = 0
        while i < len(buffer):
            chunk = buffer[i]
            # There may be multiple <think> / </think> tags in this chunk.
            # First check if </think> requires lookahead (5 chunks).
            # If the chunk itself contains multiple </think>, process them all.

            # If lookahead is needed and partial=True & fewer than 5 subsequent chunks, skip processing
            if "</think>" in chunk:
                if partial and (i + 5 >= len(buffer)):
                    # Indeterminate => keep in buffer, await more chunks or end of stream
                    break
                else:
                    # Determinable => pop from buffer for processing
                    buffer.pop(i)
                    # Check if any of the next 5 chunks contain </think>
                    next_5 = buffer[i : i + 5]
                    has_more_think = any("</think>" in c for c in next_5)
                    # If '</think>' appears within the next 5, this occurrence is not the last
                    processed = self._transform_chunk(
                        chunk, is_last_think=not has_more_think, context=context
                    )
                    finalized.append(processed)
                    continue
            else:
                # No '</think>' => can pop and process immediately
                buffer.pop(i)
                processed = self._transform_chunk(chunk, is_last_think=False, context=context)
                finalized.append(processed)
                continue

            i += 1

        return finalized

    async def _finalize_all_chunks(self, buffer, context):
        """
        Called when stream ends or on exception to force-process all chunks in buffer:
         - There are no subsequent 5 chunks for any '</think>', so all are considered the last occurrence.
         - Transform all chunks into final form and yield them.
        """
        while buffer:
            chunk = buffer.pop(0)
            # Force treat as last occurrence to prevent leftovers
            chunk = self._transform_chunk(chunk, is_last_think=True, context=context)
            yield chunk

    def _transform_chunk(self, chunk: str, is_last_think: bool, context: dict) -> str:
        # Process multiple <think>/<</think> tags within a single chunk:
        #   - First <think> => replace with "\n```Reasoning...\n"; delete subsequent ones.
        #   - '</think>' replaced with "\n```\n" if is_last_think, else removed.
        result = chunk
        # Handle multiple occurrences of <think>
        while "<think>" in result:
            idx = result.index("<think>")
            if not context["first_think_found"]:
                # On first occurrence => special replacement
                result = (
                    result[:idx] + "\n```Reasoning...\n" + result[idx + len("<think>") :]
                )
                context["first_think_found"] = True
            else:
                # Subsequent <think> => delete
                result = result[:idx] + result[idx + len("<think>") :]
        # Handle multiple occurrences of </think>
        while "</think>" in result:
            idx = result.index("</think>")
            if is_last_think:
                result = result[:idx] + "\n```\n" + result[idx + len("</think>") :]
            else:
                # If not last occurrence => remove tags
                result = result[:idx] + result[idx + len("</think>") :]
        # Remove leftover fragments in order: '/think>', 'think>', 'hink>', 'ink>', 'nk>', 'k>'
        for pattern in ["/think>", "think>", "hink>", "ink>", "nk>", "k>"]:
            result = result.replace(pattern, "")

        return result

    def _emit_status(self, description: str, done: bool = False) -> Awaitable[None]:
        if self.emitter:
            return self.emitter(
                {"type": "status", "data": {"description": description, "done": done}}
            )
        return asyncio.sleep(0)

    def _format_error(self, status_code: int, error: bytes) -> str:
        if isinstance(error, str):
            error_str = error
        else:
            error_str = error.decode(errors="ignore")
        try:
            err_msg = json.loads(error_str).get("message", error_str)[:200]
        except Exception:
            err_msg = error_str[:200]
        return json.dumps(
            {"error": f"HTTP {status_code}: {err_msg}"}, ensure_ascii=False
        )

    def _format_exception(self, e: Exception) -> str:
        tb_lines = traceback.format_exception(type(e), e, e.__traceback__)
        detailed_error = "".join(tb_lines)
        return json.dumps({"error": detailed_error}, ensure_ascii=False)
