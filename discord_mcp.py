#!/usr/bin/env python3
"""
Servidor MCP — Discord para Claude Desktop.

Permite que o Claude leia canais do Discord diretamente durante a conversa.
Configure o Claude Desktop para usar este servidor e pergunte ao Claude
sobre pendências, resumos e assuntos parados nos seus canais.
"""

import os
from datetime import datetime

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
MY_DISCORD_NAME = os.getenv("MY_DISCORD_NAME", "")
MY_DISCORD_ID = os.getenv("MY_DISCORD_ID", "")
MONITORED_CHANNELS = [
    c.strip() for c in os.getenv("MONITORED_CHANNELS", "").split(",") if c.strip()
]

API = "https://discord.com/api/v10"
HEADERS = {"Authorization": f"Bot {DISCORD_TOKEN}"}

mcp = FastMCP("Discord Assistant")


# ── Discord REST API ──────────────────────────────────────────────────────────

def _get(path: str, params: dict = None) -> dict | list:
    r = httpx.get(f"{API}{path}", headers=HEADERS, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def _format_msg(msg: dict) -> str:
    """Formata uma mensagem do Discord em texto legível."""
    author = (
        msg.get("author", {}).get("global_name")
        or msg.get("author", {}).get("username", "?")
    )
    ts_raw = msg.get("timestamp", "")
    try:
        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).strftime("%d/%m %H:%M")
    except Exception:
        ts = ts_raw[:16]

    content = msg.get("content") or "[sem texto]"

    mentions = [
        m.get("global_name") or m.get("username", "?")
        for m in msg.get("mentions", [])
    ]
    if mentions:
        content += f" [menciona: {', '.join(mentions)}]"

    if msg.get("attachments"):
        nomes = [a.get("filename", "arquivo") for a in msg["attachments"]]
        content += f" [anexos: {', '.join(nomes)}]"

    return f"[{ts}] {author}: {content}"


def _fetch_channel(channel_id: str, quantidade: int) -> tuple[str, list[str]]:
    """Retorna (nome_do_canal, lista_de_linhas_formatadas)."""
    quantidade = min(max(1, quantidade), 100)

    try:
        info = _get(f"/channels/{channel_id}")
        name = info.get("name", channel_id)
    except Exception:
        name = channel_id

    msgs = _get(f"/channels/{channel_id}/messages", params={"limit": quantidade})
    msgs.reverse()  # ordem cronológica
    return name, [_format_msg(m) for m in msgs]


def _fetch_threads(guild_id: str, channel_id: str = None, quantidade: int = 100) -> str:
    """Busca threads ativas de um servidor, opcionalmente filtradas por canal."""
    quantidade = min(max(1, quantidade), 100)

    try:
        data = _get(f"/guilds/{guild_id}/threads/active")
        threads = data.get("threads", [])
    except Exception as e:
        return f"Erro ao buscar threads: {e}"

    if channel_id:
        threads = [t for t in threads if str(t.get("parent_id")) == str(channel_id)]

    if not threads:
        return "Nenhuma thread ativa encontrada."

    parts = []
    for thread in threads:
        tid = thread.get("id")
        tname = thread.get("name", tid)
        parent_id = thread.get("parent_id", "?")

        try:
            msgs = _get(f"/channels/{tid}/messages", params={"limit": quantidade})
            msgs.reverse()
            lines = [f"  {_format_msg(m)}" for m in msgs]
            parts.append(f"  [Thread: {tname} | canal: {parent_id}]\n" + "\n".join(lines))
        except Exception as e:
            parts.append(f"  [Thread: {tname}] Erro: {e}")

    return "\n\n".join(parts)


# ── Ferramentas MCP ───────────────────────────────────────────────────────────

@mcp.tool()
def listar_canais(guild_id: str) -> str:
    """Lista todos os canais de texto de um servidor Discord.

    Use esta ferramenta quando o usuário quiser saber quais canais existem
    ou precisar do ID de um canal específico.

    Args:
        guild_id: ID numérico do servidor (guild) do Discord.
    """
    channels = _get(f"/guilds/{guild_id}/channels")
    text_channels = [c for c in channels if c.get("type") == 0]
    text_channels.sort(key=lambda c: c.get("position", 0))

    if not text_channels:
        return "Nenhum canal de texto encontrado."

    lines = [f"#{c['name']}  (ID: {c['id']})" for c in text_channels]
    return "\n".join(lines)


@mcp.tool()
def ler_canal(channel_id: str, quantidade: int = 100) -> str:
    """Lê as últimas mensagens de um canal do Discord.

    Use esta ferramenta para buscar o conteúdo de um canal específico.
    Após buscar, analise as mensagens conforme o pedido do usuário:
    resumo, pendências, assuntos parados, etc.

    Args:
        channel_id: ID numérico do canal.
        quantidade: Quantas mensagens buscar (1-100, padrão 100).
    """
    name, lines = _fetch_channel(channel_id, quantidade)
    header = f"=== #{name} ({len(lines)} mensagens) ==="
    return "\n".join([header] + lines)


@mcp.tool()
def ler_canais_monitorados(quantidade: int = 100) -> str:
    """Lê mensagens de todos os canais configurados em MONITORED_CHANNELS.

    Use esta ferramenta para fazer análises globais como briefing,
    pendências e assuntos parados em todos os canais de uma vez.
    Após buscar, analise conforme o pedido do usuário.

    Args:
        quantidade: Quantas mensagens buscar por canal (1-100, padrão 100).
    """
    if not MONITORED_CHANNELS:
        return (
            "Nenhum canal monitorado configurado.\n"
            "Adicione MONITORED_CHANNELS no arquivo .env com os IDs dos canais, "
            "separados por vírgula."
        )

    partes = []
    erros = []

    for cid in MONITORED_CHANNELS:
        try:
            name, lines = _fetch_channel(cid, quantidade)
            header = f"=== #{name} ({len(lines)} mensagens) ==="
            partes.append("\n".join([header] + lines))
        except Exception as e:
            erros.append(f"Canal {cid}: {e}")

    resultado = "\n\n".join(partes)

    if erros:
        resultado += "\n\n⚠️ Erros:\n" + "\n".join(erros)

    # Injeta contexto do usuário para o Claude usar nas análises
    if MY_DISCORD_NAME or MY_DISCORD_ID:
        user_info = f"\n\n---\nUsuário dono desta conta: {MY_DISCORD_NAME or MY_DISCORD_ID} (ID: {MY_DISCORD_ID})"
        resultado += user_info

    return resultado


@mcp.tool()
def ler_threads_servidor(guild_id: str, quantidade: int = 100) -> str:
    """Lê todas as threads ativas de um servidor Discord.

    Use quando o usuário quiser ver discussões em andamento nas threads,
    identificar pendências ou acompanhar conversas paralelas.

    Args:
        guild_id: ID numérico do servidor.
        quantidade: Quantas mensagens buscar por thread (1-100, padrão 100).
    """
    resultado = _fetch_threads(guild_id, quantidade=quantidade)
    return f"=== Threads ativas do servidor {guild_id} ===\n\n{resultado}"


@mcp.tool()
def ler_threads_canal(guild_id: str, channel_id: str, quantidade: int = 100) -> str:
    """Lê as threads ativas de um canal específico do Discord.

    Use quando o usuário quiser ver apenas as threads de um canal em particular.

    Args:
        guild_id: ID numérico do servidor.
        channel_id: ID numérico do canal pai.
        quantidade: Quantas mensagens buscar por thread (1-100, padrão 100).
    """
    resultado = _fetch_threads(guild_id, channel_id=channel_id, quantidade=quantidade)
    return f"=== Threads ativas do canal {channel_id} ===\n\n{resultado}"


@mcp.tool()
def meu_perfil() -> str:
    """Retorna as informações configuradas do usuário (nome e ID no Discord).

    Use esta ferramenta quando o Claude precisar saber quem é o dono
    da conta para identificar pendências e menções.
    """
    if not MY_DISCORD_NAME and not MY_DISCORD_ID:
        return (
            "Usuário não configurado.\n"
            "Adicione MY_DISCORD_NAME e MY_DISCORD_ID no arquivo .env."
        )
    return (
        f"Nome no Discord: {MY_DISCORD_NAME}\n"
        f"ID no Discord: {MY_DISCORD_ID}"
    )


if __name__ == "__main__":
    mcp.run()
