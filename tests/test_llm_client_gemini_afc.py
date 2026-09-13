"""Le relais Gemini de call_llm désactive l'appel automatique de fonctions."""

from __future__ import annotations

import inspect
import re

from core import llm_client


def test_generate_content_config_disables_afc():
    source = inspect.getsource(llm_client)
    i = source.index("tools=gen_tools, max_output_tokens=800")
    block = source[i:source.index(")", i) + 1]
    assert re.search(r"automatic_function_calling=.*disable=True", block)
