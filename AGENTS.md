# Guia AGENTS - vivocrawler
Este guia e para agentes de codigo que trabalham neste repositorio.
Siga estes padroes, a menos que o usuario peca diferente.
## 1) Contexto do projeto
- Linguagem/runtime: Python 3.11 (`.python-version` = `3.11.14`).
- Dominio: automacao de login no Vivo Empresas + download de faturas PDF.
- Scripts principais:
  - `vivo_download.py` (fluxo principal ponta a ponta com Scrapling/Camoufox + XPath)
- Ferramentas em `pyproject.toml`: `pytest`, `ruff`, `mypy`.
## 2) Comandos de setup
Use o setup padrao do repositorio:
```bash
pyenv local 3.11.14
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m playwright install chromium
```
Notas:
- `requirements-dev.txt` inclui dependencias de runtime + desenvolvimento.
- A instalacao do navegador do Playwright e obrigatoria antes de rodar scripts Playwright.
## 3) Comandos de build, lint e testes
Nao ha etapa de build de pacote; e um projeto baseado em scripts.
### 3.1 Checagem de sintaxe
```bash
python -m py_compile *.py
```
### 3.2 Lint
```bash
ruff check .
```
Auto-correcao opcional:
```bash
ruff check . --fix
```
### 3.3 Checagem de tipos
```bash
mypy *.py
```
### 3.4 Execucao completa de testes
```bash
pytest
```
Importante: `pytest` usa `testpaths = ["tests"]`, entao `pytest` simples so auto-coleta testes em `tests/`.
### 3.5 Rodar um teste unico (importante)
Rodar um arquivo de teste:
```bash
pytest tests/test_file.py
```
Rodar uma funcao de teste:
```bash
pytest tests/test_file.py::test_specific_behavior
```
Rodar um metodo de classe de teste:
```bash
pytest tests/test_file.py::TestFlow::test_download_retry
```
Rodar testes por palavra-chave:
```bash
pytest -k "download and retry"
```
Rodar explicitamente um arquivo de teste no nivel raiz:
```bash
pytest vivo_download.py
```
## 4) Comandos de execucao (fluxos manuais)
Fluxo principal:
```bash
python vivo_download.py
```
Fluxo apenas de listagem:
```bash
python vivo_download.py --listar
```
Fluxo com debug (salva HTML e screenshots de cada etapa):
```bash
python vivo_download.py --debug
```
## 5) Estilo de codigo e convencoes
### 5.1 Imports
- Mantenha imports no topo do arquivo.
- Ordem de grupos: biblioteca padrao, depois terceiros.
- Prefira um import por linha em `from ... import ...`.
- Deixe o Ruff (`I`) aplicar a ordenacao de imports.
### 5.2 Formatacao
- Indentacao de 4 espacos.
- Tamanho maximo de linha: 100.
- Mantenha funcoes focadas; extraia helpers para logica de UI repetida.
- Prefira `pathlib.Path` em vez de concatenacao manual de strings de caminho.
### 5.3 Tipagem
- Adicione type hints em funcoes novas/alteradas quando pratico.
- Prefira containers concretos (`list[str]`, `dict[str, str]`).
- Destaques da configuracao do mypy:
  - `python_version = 3.11`
  - `warn_return_any = true`
  - `ignore_missing_imports = true`
  - `disallow_untyped_defs = false`
### 5.4 Nomenclatura
- Modulos/funcoes: `snake_case`.
- Constantes: `UPPER_SNAKE_CASE`.
- Flags de CLI: `kebab-case` (`--account-number`, `--wait-ms`).
- Use nomes explicitos ligados a intencao da automacao (`normalize_cpf`, `save_download_object`).
### 5.5 Estrutura de CLI
- Prefira este formato nos scripts:
  - `parse_args() -> argparse.Namespace`
  - `main() -> None`
  - `if __name__ == "__main__": main()`
- Use variaveis de ambiente para segredos/defaults quando possivel (`VIVO_CPF`, `VIVO_PASSWORD`).
### 5.6 Tratamento de erros
- UI de browser e instavel: retries e fallbacks protegidos sao aceitos.
- Capture excecoes especificas quando possivel.
- Se `except Exception` amplo for necessario, mantenha bloco pequeno e registre contexto (`[warn]`).
- Nunca ignore silenciosamente falhas que possam invalidar o resultado final.
- Aprendizado pratico: executar `page.content()` e `page.screenshot()` durante etapas criticas ajuda a estabilizar o crawler mesmo quando o resultado nao e salvo em disco.
### 5.7 Estilo de logs/saida
- Mantenha os prefixos de print existentes:
  - `[info]` progress
  - `[ok]` successful step
  - `[warn]` recoverable issue
  - `[done]` final completion
### 5.8 Sistema de arquivos/artefatos
- Mantenha faturas baixadas em `downloads/vivo`.
- Mantenha screenshots/metadados em `screenshots/...`.
- Sempre crie diretorios de saida com `mkdir(parents=True, exist_ok=True)`.
- Grave JSON em formato amigavel para maquina (`ensure_ascii=True`, `indent=2`).
- No `vivo_download.py`, sem `--debug` nao salvar snapshots/HTML; com `--debug` salvar ambos para diagnostico.
## 6) Escopo de Ruff e mypy
- Regras do Ruff: `E`, `F`, `I`, `B`, `UP`.
- Exclusoes de lint/tipo incluem `.venv`, `downloads` e `screenshots`.
## 7) Expectativas de teste para agentes
- Para edicoes de codigo, rode ao menos `ruff check .`.
- Rode `mypy *.py` ao mexer em logica tipada ou assinaturas.
- Prefira testes direcionados primeiro, depois suite mais ampla quando relevante.
## 8) Arquivos de regra Cursor/Copilot
Repositorio verificado para arquivos adicionais de instrucao de agente:
- `.cursor/rules/`: not present
- `.cursorrules`: not present
- `.github/copilot-instructions.md`: not present
Se algum deles for adicionado depois, trate como instrucao local de maior prioridade.
## 9) Seguranca e segredos
- Nunca hardcode CPF/senha ou outras credenciais.
- Prefira variaveis de ambiente (`VIVO_CPF`, `VIVO_PASSWORD`) para dados sensiveis.
- Nao comite `.env`, faturas baixadas ou screenshots sem pedido explicito.
