# Investigação: rodar o crawler-vivo fora do Mac (VPS / servidor)

Registro do que foi tentado para fazer o crawler rodar no servidor **contabo001**, por que
não funcionou, e qual é o caminho recomendado. Data: 2026-08-03.

## TL;DR

- **Roda 100% no Mac** (Móvel e Fixo). É o caminho recomendado.
- **Não roda de forma confiável no VPS** (contabo001): o antifraude web da Vivo (`s.dnofd.com`)
  reprova o ambiente de datacenter e responde um **falso "A senha não está correta"**.
- **App Android também não é atalho viável**: tem API REST nativa própria, mas está trancado por
  endpoints cifrados + assinatura nativa (`libsigner`) + antifraude **Topaz** com anti-Frida/anti-emulador.
- **Senha atual no `.env`:** `C@mila12` (trocada de `Cocada28` no início da sessão).

---

## 1. Troca de senha (feito, funcionando)

- `.env`: `VIVO_PASSWORD` atualizado `Cocada28` → `C@mila12`.
- Testado no Mac: `python vivo_movel.py --listar` e `python vivo_fixo.py --listar` → OK.
  - Móvel: CNPJ 10755237000136, 2 contas, 12 faturas, 8 PDFs baixados.
  - Fixo: conta 899933550000, 2 boletos (202606 R$ 97,16 / 202607 R$ 94,44).

## 2. Deploy no contabo001 (instalado, mas login instável)

**Acesso:** `ssh -p 17500 igor@10.203.0.3` (Ubuntu, Python 3.12.3).

**Feito:**
- Projeto copiado via rsync para `~/projects/crawler-vivo`.
- venv + `pip install -r requirements.txt` + `python -m camoufox fetch`.
- `python3.12-venv` instalado (faltava `ensurepip`).
- `.env` com a senha nova presente no servidor.

**Proxy de saída (funciona):**
- tinyproxy no Mac em `10.202.0.3:8888` (WireGuard). Projeto em `~/projects/proxy` (`./start.sh`).
- No servidor: `VIVO_PROXY=http://10.202.0.3:8888` → tráfego sai pelo IP residencial do Mac.
- **Suporte a proxy adicionado ao código** (`vivo_core.executar_fetch`): lê `VIVO_PROXY` /
  `HTTPS_PROXY`, liga `geoip=True` automaticamente. Também `VIVO_CAMOUFOX_OS` (ex.: `macos`)
  para forçar fingerprint de OS.
- Atenção: roteador da loja tem 2 WANs; IP público pode flapar (186.195.35.238 / 131.0.23.168).

**Resultado:** login instável — **1 sucesso em ~5 execuções**. Móvel passou 1 vez (12 faturas,
12 PDFs); Fixo falhou várias, inclusive isolado com intervalo. Falso "senha não está correta".

**Dependências que ajudaram (mas não estabilizaram) — instaladas no contabo001:**
```bash
sudo apt-get install -y libgl1 libegl1 libgl1-mesa-dri mesa-utils xvfb   # WebGL por software (llvmpipe)
sudo apt-get install -y fonts-liberation fonts-dejavu fonts-noto-core fonts-freefont-ttf fonts-croscore fontconfig
sudo apt-get install -y speech-dispatcher espeak-ng pulseaudio            # vozes TTS (era 0)
```
E rodar sempre com display virtual: `... vivo_movel.py --mode virtual` (headless puro fica sem WebGL).

## 3. Diagnóstico de fingerprint (Mac vs VPS)

Dump próprio do fingerprint do Camoufox nas duas máquinas (sem tocar a Vivo). 48/81 sinais
síncronos idênticos. Diferenças relevantes:

| Sinal | Mac (passa) | contabo001 (barra) | Corrigido? |
|---|---|---|---|
| WebGL | Apple M1 | ausente → NVIDIA GTX 980 (spoof llvmpipe) | Mesa+xvfb |
| Fontes | 16 | 2–3 | fontes instaladas (pouco efeito) |
| Vozes TTS | 177 | 0 → 13362 | speech-dispatcher (exótico) |
| `enumerateDevices()` | instantâneo | **trava (timeout)** | patch JS (ver abaixo) |
| WebRTC ICE | 1 cand | 3 cands | não |
| UA / locale | macOS / en-US | Linux / pt-BR | esperado |

**Descoberta forte:** `navigator.mediaDevices.enumerateDevices()` **trava** no Camoufox/Linux
(browserforge marca `multimediaDevices` como *Unsupported*). Corrigido por patch JS injetado
(`vivo_core.aplicar_patch_media_devices`, `add_init_script`+`reload`, `VIVO_FIX_MEDIA_DEVICES=auto|on|off`):
resolve em 1ms com 3 devices, vale no main world, `toString` continua nativo. **Mas não destravou
o login** — não era (só) esse o sinal.

## 4. Como o portal web autentica (captura de rede no Mac)

- Login: `POST https://auth.vivo.com.br/LoginB2B2/login/mask`
- **`POST https://s.dnofd.com/mve/ss`** — beacon de antifraude de terceiro; é ele que pontua a sessão.
- Dados (API JSON limpa): `GET https://mve.vivo.com.br/sec/module/invoices-grid/list/?documentNumber=...&offset=0&limit=4`
- Só o **login** é vigiado; os **dados** são API REST simples atrás de cookie de sessão (com heartbeat).

## 5. App Android — recon (skill android-app-recon)

**APK:** puxado do celular físico (Galaxy S24+, sem root) via `adb pull`:
`br.com.vivo.meuvivoempresas` (base 41MB + splits arm64_v8a/pt/xxhdpi). Play Store recusa instalar
no emulador ("item not found").

**Estática:**
- App **nativo Kotlin** (4 dex), **NÃO Flutter, NÃO webview**. Usa **Retrofit2 + OkHttp**, header
  `Authorization` (Bearer) → **existe API REST mobile**.
- URLs da Vivo **cifradas** nos dex (libs `libmcrypt`, `libsigner`, `libiavoashcn` ofuscada).
- Hosts confiáveis (network_security_config): `auth.vivo.com.br`, `mve.vivo.com.br` (mesmo backend
  do web!), `api-mve-aem.vivo.com.br`, `vve.vivo.com.br`.
- Antifraude/biometria mobile = **Topaz** (`br.com.topaz.heartbeat`, OCR/FaceAuthorization).
  Diferente do beacon web `dnofd`.

**Dinâmica (emulador `tuya_root`, arm64, com root):**
- APK instalou e abriu, mas **trava (ANR/crash) no onboarding, antes do login** — anti-emulador do Topaz.
- tcpdump on-device pegou só telemetria de boot (clixsight/Topaz, firebase, adobe). Login não ocorreu.

## 6. Tentativa com Frida (bypass do Topaz)

Frida-server 17.16.4 (renomeado `sysmon`, porta 47777 para evitar detecção). ~10 tentativas:

- Bypass SSL pinning (SSLContext / CertificatePinner / Conscrypt) — instala OK.
- Hook nativo **BoringSSL** `SSL_read`/`SSL_write` (o OkHttp está ofuscado por R8 — `addInterceptor`
  virou caractere unicode, hook por nome Java não funciona).
- Neutralização de terminação: `exit`/`_exit`/`abort`/`raise`/`kill`/`tgkill`/`tkill`/`pthread_kill`
  (incl. sinais RT **34-39** = SIGRTMIN+, o vetor típico de anti-tamper), `syscall exit_group`,
  ptrace=0, TracerPid spoof, porta Frida, + Java `System.exit`/`killProcess`/`finish`/`stopSelf`.

**Resultado:** o Topaz **mata e reinicia** o app num PID novo, fora do Frida, **antes de qualquer
request TLS**. Bloqueando tudo → `NullPointerException` (efeito colateral dos hooks). Versão
cirúrgica (só fatais) → app termina **sem disparar nenhum hook** de kill/exit/raise/tgkill/syscall.
Conclusão: terminação via **SVC inline** (instrução de syscall no assembly, fora do alcance de
Interceptor de runtime). **Zero tráfego TLS capturado.**

**Pesquisa de fóruns aplicada:** spentera, apkunpacker/FridaScripts (StopExit.js, AntiDebug.js),
kayssel (patch nativo). Confirmam: caso de SVC inline só cede com **patch ESTÁTICO da lib nativa**
(Ghidra: achar a checagem, NOP/return, repackage+resign) — projeto de horas em libs ofuscadas, e
MESMO vencendo restaria replicar a assinatura `libsigner`. Ferramentas: FridaBypassKit (okankurtuluss).

## 7. Veredito e recomendação

| Caminho | Status |
|---|---|
| **Mac** | ✅ 100% estável (Móvel + Fixo) |
| **VPS web (contabo001)** | ❌ antifraude `dnofd` reprova; só Camoufox passa no Cloudflare, mas sessão Linux+residencial é rejeitada |
| **App Android** | ❌ trancado: endpoints cifrados + `libsigner` + Topaz anti-Frida/anti-emu |

**Recomendado:** rodar no **Mac** e o **contabo001 dispara por ssh** (cron no Mac, ou contabo
aciona o Mac). Sem engenharia reversa, sem risco pra conta Vivo.

**Cuidado com lockout:** cada login falho consome contador da Vivo ("Você tem mais 3 tentativas");
um login bem-sucedido (Mac ou app) zera. Não encadear tentativas de login do VPS sem zerar antes.

## 8. Mudanças de código nesta sessão (ainda não commitadas)

- `vivo_core.executar_fetch`: suporte a proxy (`VIVO_PROXY`/`HTTPS_PROXY` + `geoip` auto) e
  fingerprint de OS (`VIVO_CAMOUFOX_OS`).
- `vivo_core.aplicar_patch_media_devices` + chamada em `_page_action`: patch de `enumerateDevices`
  em Linux (`VIVO_FIX_MEDIA_DEVICES=auto|on|off`).
- `README.md`: seção "Servidor Linux headless (VPS)" com dependências e avisos.
- (Já havia WIP anterior: resumo "FATURAS EM ATRASO", regex de barcode Fixo, detecção de situação Móvel.)

## 9. Re-investigação 2026-08-03 (sessão 2): túnel WG, engines, warm-up

### 9.1 Topologia WireGuard (por que o "caminho via Mac" falha)

VPS e Mac são **peers de um hub WireGuard** em `145.223.92.63:51820` (Contabo, `wg0` do servidor),
**não** endpoints diretos entre si. O hub NATa o tráfego de internet dos peers com o próprio IP
de datacenter. Consequência: forçar rota via túnel para o Mac nunca chega nele — o hub responde
primeiro. Tentativa de pf NAT (masquerade) no Mac foi revertida (anchors, forwarding, allowed-ips).

- Exits: Mac direto = `186.195.35.238`; VPS direto = `167.86.123.213`;
  VPS via `VIVO_PROXY=http://10.202.0.3:8888` (tinyproxy do Mac) = `186.195.35.238` (mesmo IP do Mac).
- Acesso ao Mac a partir do próprio Mac por `10.202.0.3:8888` é contaminado (loop WG); do VPS funciona.

### 9.2 Teste de engines contra mve.vivo.com.br

| Engine | Cloudflare "Um momento…" | Resultado |
|---|---|---|
| Camoufox | passa | Mac: login OK (12 faturas). VPS+residencial: CPF/senha preenchem, mas dashboard **não** detectado |
| patchright (`channel=chrome`) | preso | não monta o form |
| nodriver (CDP direto) | preso | campo CPF não aparece (Mac e VPS via proxy) |

### 9.3 Conclusão do antifraude

Com o MESMO IP residencial de saída do Mac (186.195.35.238) o login no VPS é rejeitado:
o antifraude `s.dnofd.com` distingue a sessão por **fingerprint (Linux + proxy residencial)**,
não só por IP. Camoufox é a única engine que atravessa o Cloudflare, mas perde na pontuação
de ambiente no Linux. `--warmup-ms` (página neutra por 20s antes do login) não ajudou — na
verdade o campo de senha deixou de aparecer.

### 9.4 Estado do VPS (pronto para experimentos futuros)

- `~/crawler-vivo` com venv (Python 3.12.3), `camoufox fetch`, GeoLite2-City.mmdb baixado
  manualmente (64M; a API do GitHub rate-limita o downloader com 403), `.env` presente.
- `google-chrome` instalado (`/usr/bin/google-chrome`); nodriver adicionado ao
  `requirements-dev.txt` (instalado no venv do VPS).
- Novo no código: `--warmup-ms` (env `VIVO_WARMUP_MS`), `_NdTabAdapter.goto`.

## 10. BREAKTHROUGH 2026-08-04: Camoufox 0.5.4 + presets macOS reais

### 10.1 Pesquisa de browsers headless 2026 (resumo)

- Benchmark ianlpaterson (maio/2026, 7 tools × 31 alvos): **nodriver** (CDP direto, sem shim
  do Playwright) é o único com 0 bloqueios; Patchright/Camoufox/CloakBrowser ~25; rebrowser = vanilla.
- **"Shape coherence"**: o gate cruza camadas (TLS/JA4, HTTP/2, navigator, canvas, GPU). Proxy só
  reescreve o IP; "um servidor Linux atrás de proxy residencial fabrica uma contradição que o gate
  usa contra você". É EXATAMENTE o que bloqueava o VPS.
- Camoufox **0.5.x** (Firefox ≥ 149) adicionou **fingerprint presets reais** (`fingerprint-presets-v150.json`:
  312 presets reais coletados de tráfego real: 67 macOS / 180 Windows / 65 Linux), com fontes de
  sistema incluídas e WebGL spoofing. É o que destrava o VPS.
- CloakBrowser (Chromium C++ patched, 49 patches) e Celery/playwright-stealth v2 existem, mas não
  foram necessários após o preset macOS funcionar.

### 10.2 O que destravou o VPS

No VPS (Camoufox 0.5.4 + Firefox **152.0.4-beta.28**, já instalado):
```
VIVO_PROXY=http://10.202.0.3:8888   # proxy residencial do Mac
VIVO_CAMOUFOX_OS=macos              # fingerprint de OS
VIVO_CAMOUFOX_PRESET=51             # pin no preset macOS v150 (Apple M1, 1512x982)
```
Resultado do fingerprint no VPS: `platform=MacIntel`, UA macOS Firefox 152, `pt-BR`,
`America/Sao_Paulo`, tela 1512x982, 10 cores — um "MacBook brasileiro" coerente.
**Primeiro login VPS bem-sucedido da história** (dashboard detectado).

- Antes: Camoufox no VPS = fingerprint Linux → antifraude rejeitava (falso OAM-2) mesmo com IP
  residencial idêntico ao Mac. O problema era o **shape (OS), não o IP**.
- Preset `on/random` pode crashar: presets com vendor WebGL fora da DB do Camoufox →
  `No WebGL data found for vendor "..."`. Por isso o default do código é **pin 51** (Apple M1).

### 10.3 Confiabilidade e retry

- O antifraude pontua por **reputação/volume do IP na janela**: a 1ª tentativa após cooldown passa
  (14:11 e 14:27 de 2026-08-04); tentativas rápidas subsequentes falham com **falso OAM-2**
  (`auth.vivo.com.br/.../authz?...p_error_code=OAM-2`).
- **OAM-2 NÃO trava a conta**: após 8+ falhas no VPS num dia, o login no Mac segue normal. Retry
  é seguro.
- Código adicionado: `VIVO_LOGIN_RETRIES` (default 3) + `VIVO_LOGIN_RETRY_BACKOFF` (default 60s)
  no `executar_fetch` (camoufox): re-tenta com nova sessão/fingerprint até o dashboard detectar.
  No teste do dia (IP bem flagado, ~1/4 de chance): 3 falhas OAM-2 → a 4ª tentativa passou.
- Uso produção (1 login/dia, reputação fresca): expectativa de passar na 1ª tentativa.

### 10.4 Receita VPS (funciona)

```bash
cd ~/crawler-vivo && set -a && source .env && set +a
VIVO_PROXY=http://10.202.0.3:8888 \
VIVO_CAMOUFOX_OS=macos \
VIVO_LOGIN_RETRIES=3 VIVO_LOGIN_RETRY_BACKOFF=90 \
.venv/bin/python vivo_movel.py --listar --engine camoufox --mode virtual
```
- `--mode virtual` (xvfb) é necessário no Linux para WebGL.
- Cuidado: o IP residencial é compartilhado com o Mac; muitos logins no dia flagam o IP para o
  resto do dia. Não testar em rajada sem cooldown.


