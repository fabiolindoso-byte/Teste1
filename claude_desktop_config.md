# Configuração do Claude Desktop

Para conectar o Claude Desktop ao Discord, adicione o bloco abaixo
no arquivo de configuração do Claude Desktop.

---

## Onde fica o arquivo de configuração

**Windows:**
```
C:\Users\SeuUsuario\AppData\Roaming\Claude\claude_desktop_config.json
```

**Mac:**
```
~/Library/Application Support/Claude/claude_desktop_config.json
```

---

## O que adicionar no arquivo

Abra o arquivo (crie se não existir) e coloque:

```json
{
  "mcpServers": {
    "discord": {
      "command": "python",
      "args": ["C:\\Users\\SeuUsuario\\Desktop\\Teste1\\discord_mcp.py"]
    }
  }
}
```

⚠️ Ajuste o caminho `C:\\Users\\SeuUsuario\\Desktop\\Teste1\\discord_mcp.py`
para o caminho real onde você salvou o projeto.

No Windows, use barras duplas `\\` no caminho.

---

## Verificar se funcionou

1. Salve o arquivo de configuração
2. **Feche e reabra** o Claude Desktop
3. Abra uma nova conversa
4. Se aparecer um ícone de ferramentas (🔧) ou "discord" nas integrações, funcionou

---

## Como usar

Com o MCP configurado, basta perguntar ao Claude normalmente:

- "O que está pendente pra mim no Discord?"
- "Me resume o canal ID 1370870792770031717"
- "Tem alguma coisa parada nos meus canais monitorados?"
- "Quais canais existem no servidor ID xxxx?"
- "Faz um briefing completo dos meus canais"
