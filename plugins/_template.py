"""Copiez ce fichier sous un autre nom pour créer un plugin de confiance."""

PLUGIN = {
    "name": "mon_plugin",
    "description": "Explique précisément à Gemini quand utiliser ce plugin.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "texte": {"type": "STRING", "description": "Texte à traiter"},
        },
        "required": ["texte"],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    return f"Plugin reçu : {parameters.get('texte', '')}"
