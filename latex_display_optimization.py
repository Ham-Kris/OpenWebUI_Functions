"""
title: LaTeX Display Optimization
author: Kris Ham
auther_email: krisham@mail.com
funding_url: https://www.medicnex.com/Alipay.png
description: >
    This Filter is used to detect LaTeX expressions in Open WebUI model outputs
    (supports $$...$$, $...$, \[...\] and \( ... \) formats) and add a space
    before and after each expression, enhancing the readability of formulas.
required_open_webui_version: 0.4.0
requirements:
version: 1.0.0
license: MIT
"""

import re
from pydantic import BaseModel, Field
from typing import Optional


class Filter:
    class Valves(BaseModel):
        # Whether to enable this filter; can be controlled via the Open WebUI GUI.
        enabled: bool = Field(True, description="Enable the LaTeX spacing filter")

    def __init__(self):
        self.valves = self.Valves()

    def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        # This filter does not modify input data.
        return body

    def stream(self, event: dict) -> dict:
        # For streaming output, this filter does not perform real-time modifications; simply returns the raw data.
        return event

    def outlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        """
        Process all message contents in the model output by detecting LaTeX expressions
        (in formats: $$...$$, $...$, \[...\], and \( ... \)) and adding a space before
        and after each expression.
        """
        if not self.valves.enabled:
            return body

        messages = body.get("messages", [])
        for message in messages:
            # Process only 'assistant' messages.
            if message.get("role") == "assistant" and isinstance(
                message.get("content"), str
            ):
                message["content"] = self._add_spaces_to_latex(message["content"])
        return body

    def _add_spaces_to_latex(self, text: str) -> str:
        """
        Internal method: Detects LaTeX expressions in the text and adds spaces before and after them.
        Supports formats: $$...$$, $...$, \[...\] and \( ... \)
        """
        # Process $$...$$ format first to avoid false matches by the single $...$ regex.
        text = re.sub(r"(\$\$.*?\$\$)", r" \1 ", text)
        # Process $...$ format using negative lookbehind and lookahead to avoid matching $$...$$.
        text = re.sub(r"(?<!\$)(\$.*?\$)(?!\$)", r" \1 ", text)
        # Process \[...\] format.
        text = re.sub(r"(\\\[.*?\\\])", r" \1 ", text)
        # Process \( ... \) format.
        text = re.sub(r"(\\\(.*?\\\))", r" \1 ", text)
        return text
