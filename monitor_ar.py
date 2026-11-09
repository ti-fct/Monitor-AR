#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
monitor_ar.py — Coletor de ar-condicionado VRF Midea (CCM-15) — FCT/UFG

Consulta periodicamente o status.xml de cada controlador CCM-15, decodifica o
payload hexadecimal de cada evaporadora (estado on/off, temperatura ambiente e
temperatura setada) e grava as mudanças de estado no PostgreSQL.

Características:
  - Multi-central: as centrais são definidas em CENTRAIS_JSON (.env), sem
    duplicar código (Ala A id=1 -> IDs globais 100-163; Ala B id=2 -> 200-263)
  - ID global do aparelho = id_central * 100 + numero_da_tag
    (evita colisão da numeração 0-63 entre CCMs)
  - Grava somente mudanças de estado; keep-alive a cada FORCAR_GRAVACAO_MINUTOS
  - Isolamento de falhas: uma central fora do ar não interrompe as demais
  - Proteção de FK: aparelho sem cadastro em 'aparelhos' não é gravado no histórico
  - Fuso horário: a sessão PostgreSQL e os logs rodam em America/Sao_Paulo —
    os timestamps são gravados em HORA LOCAL, mesmo com o servidor em UTC
    (conferido em 09/nov/2026: NOW() = 14:40+00 com relógio local 11:40)

Decodificação do payload (7 bytes, validada contra o midea.js oficial do CCM-15):
  Byte 0: bit 0 = unidade de temperatura (0 = °C, 1 = °F)
  Byte 3: bits 2-4 = modo (0=Cool, 1=Heat, 2/3=Fan, 4=Off, 5=Auto, 6=Offline, 7=Falha)
  Byte 4: bits 3-7 = temperatura setada
          °C: valor DIRETO (17-30) — NÃO somar +16 (o +16 do JS vale só p/ °F)
          °F: valor + 62
  Byte 6: temperatura ambiente (complemento de dois, pode ser negativa)

Configuração exclusivamente via .env (python-dotenv). Veja .env.example.
"""

import csv
import json
import logging
import os
import re
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from xml.etree import ElementTree as ET

try:
    from zoneinfo import ZoneInfo
except ImportError:          # Python < 3.9
    ZoneInfo = None

import requests
import psycopg2
from psycopg2.extras import execute_batch
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuração (carregada do .env)
# ---------------------------------------------------------------------------
load_dotenv()

DB_CONFIG = {
    'dbname': os.getenv('DB_NAME', 'ar_condicionado_db'),
    'user': os.getenv('DB_USER', 'suporte'),
    'password': os.getenv('DB_PASSWORD', ''),
    'host': os.getenv('DB_HOST', 'localhost'),
    'port': os.getenv('DB_PORT', '5432'),
    # Fuso da sessão: faz NOW()/DEFAULT NOW() gravarem HORA LOCAL na coluna
    # "timestamp" (TIMESTAMP sem fuso), mesmo com o servidor configurado em UTC.
    # Sobrescreva via DB_OPTIONS no .env se necessário.
    'options': os.getenv('DB_OPTIONS', '-c timezone=America/Sao_Paulo'),
}

INTERVALO_SEGUNDOS = int(os.getenv('INTERVALO_SEGUNDOS', '60'))
FORCAR_GRAVACAO_MINUTOS = int(os.getenv('FORCAR_GRAVACAO_MINUTOS', '60'))

# Exemplo de CENTRAIS_JSON:
# [{"id": 1, "nome": "ALA_A", "url": "http://192.168.0.12/status.xml",
#   "csv": "Identificação do sistema de ar condicionado - ALA_A.csv"},
#  {"id": 2, "nome": "ALA_B", "url": "http://192.168.0.13/status.xml",
#   "csv": "Identificação do sistema de ar condicionado - ALA_B.csv"}]
try:
    CENTRAIS = json.loads(os.getenv('CENTRAIS_JSON', '[]'))
except json.JSONDecodeError as e:
    print(f'ERRO: CENTRAIS_JSON mal formada no .env ({e})')
    raise SystemExit(1)

TIMEOUT_HTTP = 10        # timeout de leitura do status.xml (segundos)
TAGS_POR_CENTRAL = 64    # tags a0..a63 do CCM-15
BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # pasta do monitor_ar.py

LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()             # DEBUG = detalhes por ciclo
HEARTBEAT_MINUTOS = int(os.getenv('HEARTBEAT_MINUTOS', '15'))  # resumo periódico; 0 desliga
FUSO_LOG = os.getenv('FUSO_LOG', 'America/Sao_Paulo')          # fuso dos timestamps do log

# ---------------------------------------------------------------------------
# Logging (arquivo rotativo: 5 MB x 3 backups)
# ---------------------------------------------------------------------------
def _agora_fuso_log(segundos=None):
    """struct_time no fuso FUSO_LOG — mantém os timestamps do log na hora local
    mesmo que o sistema operacional rode em UTC (alinha monitor_ar.log com o banco)."""
    if segundos is None:
        segundos = time.time()
    try:
        if ZoneInfo is None:
            raise RuntimeError('zoneinfo indisponível')
        return datetime.fromtimestamp(segundos, ZoneInfo(FUSO_LOG)).timetuple()
    except Exception:
        return time.localtime(segundos)


_formatador = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
_formatador.converter = _agora_fuso_log
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler('monitor_ar.log', maxBytes=5 * 1024 * 1024,
                            backupCount=3, encoding='utf-8'),
    ],
)
for _handler in logging.getLogger().handlers:
    _handler.setFormatter(_formatador)
logger = logging.getLogger('monitor_ar')


# ---------------------------------------------------------------------------
# Configuração / identificação
# ---------------------------------------------------------------------------
def validar_config():
    """Valida a configuração antes de iniciar; encerra o processo em caso de erro.

    Deve ser chamada ANTES de init_db(): nada é criado/alterado no banco se a
    configuração estiver errada.
    """
    erros = []

    if not DB_CONFIG.get('password'):
        erros.append('DB_PASSWORD não definida no .env')

    if INTERVALO_SEGUNDOS < 5:
        erros.append('INTERVALO_SEGUNDOS deve ser >= 5')

    if not CENTRAIS:
        erros.append('CENTRAIS_JSON não definida ou vazia no .env')

    ids_vistos = set()
    for c in CENTRAIS:
        cid = c.get('id')
        nome = c.get('nome', str(cid))
        if not isinstance(cid, int) or cid < 1:
            # id=0 faria o id_global colapsar no número bruto da tag (0-63),
            # causando violação de FK — foi exatamente o erro observado antes.
            erros.append(f"Central '{nome}': id inválido ({cid!r}) — use inteiro >= 1")
        elif cid in ids_vistos:
            erros.append(f"Central '{nome}': id {cid} duplicado na CENTRAIS_JSON")
        else:
            ids_vistos.add(cid)

        if not str(c.get('url', '')).startswith('http'):
            erros.append(f"Central '{nome}': URL ausente ou inválida")

    if erros:
        for erro in erros:
            logger.error('Configuração inválida: %s', erro)
        raise SystemExit(1)

    logger.info(
        'Configuração OK — %d central(is): %s | intervalo=%ds | keep-alive=%dmin',
        len(CENTRAIS),
        ', '.join(f"{c['id']}={c.get('nome', '?')}" for c in CENTRAIS),
        INTERVALO_SEGUNDOS,
        FORCAR_GRAVACAO_MINUTOS,
    )


def id_global(central_id, numero_tag):
    """ID único do aparelho no banco.

    Central id=1 -> tags 0-63 viram 100-163; central id=2 -> 200-263; etc.
    """
    return central_id * 100 + numero_tag


# ---------------------------------------------------------------------------
# Decodificação do payload
# ---------------------------------------------------------------------------
def parse_ac_payload(hex_code):
    """Decodifica o payload hexadecimal de 7 bytes (14 caracteres) de uma evaporadora.

    Layout validado contra o midea.js oficial do CCM-15:
      Byte 0: bit 0 = unidade (0 = °C, 1 = °F)
      Byte 3: bits 2-4 = modo (0=Cool, 1=Heat, 2/3=Fan, 4=Off, 5=Auto, 6=Offline, 7=Falha)
      Byte 4: bits 3-7 = temperatura setada
              °C: valor DIRETO (17-30) — sem +16 (o +16 do JS aplica-se só a °F)
              °F: valor + 62
      Byte 6: temperatura ambiente (complemento de dois — pode ser negativa)

    Retorna {'is_on': 0/1, 'temp_ambiente': int, 'temp_setada': int} ou None.
    """
    if not hex_code or hex_code == '-' or len(hex_code) < 14:
        return None
    try:
        hex_limpo = hex_code.strip().rstrip(',')
        if len(hex_limpo) < 14:
            return None
        dados = bytes.fromhex(hex_limpo)

        eh_fahrenheit = bool(dados[0] & 0x01)

        modo = (dados[3] >> 2) & 0x07
        is_on = 0 if modo in (4, 6, 7) else 1   # Off / Offline / Falha

        bruto_set = (dados[4] >> 3) & 0x1F
        temp_setada = bruto_set + 62 if eh_fahrenheit else bruto_set

        bruto_amb = dados[6]
        temp_ambiente = bruto_amb if bruto_amb < 128 else bruto_amb - 256

        return {'is_on': is_on, 'temp_ambiente': temp_ambiente, 'temp_setada': temp_setada}
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Banco de dados
# ---------------------------------------------------------------------------
def get_connection():
    return psycopg2.connect(**DB_CONFIG)


def garantir_conexao(conn):
    """Reabre a conexão se ela foi perdida; retorna a conexão pronta para uso."""
    try:
        if conn is None or conn.closed:
            return get_connection()
        with conn.cursor() as cur:
            cur.execute('SELECT 1')
        return conn
    except psycopg2.Error:
        try:
            conn.close()
        except Exception:
            pass
        return get_connection()


def carregar_ids_cadastrados(conn):
    """Retorna o conjunto de IDs globais cadastrados em 'aparelhos' (proteção de FK)."""
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT id_central FROM aparelhos')
            return {r[0] for r in cur.fetchall()}
    except psycopg2.Error as e:
        logger.error('Erro ao consultar aparelhos cadastrados: %s', e)
        return set()


def ler_csv_aparelhos(caminho):
    """Lê o CSV de identificação da central e retorna lista de (tag, id_aparelho, local).

    Aceita as colunas em QUALQUER ordem quando há cabeçalho (case-insensitive):
    'id_central_monitoramento' (tag aN do status.xml, 0-63; zeros à esquerda OK,
    ex.: '00' -> tag 0), 'id_aparelho' e 'local'. Sem cabeçalho, assume a ordem
    tag,id_aparelho,local. Sem cabeçalho reconhecível, aplica heurística:
    a única célula numérica 0-63 de cada linha é a tag.

    Retorna None se não decodificar o arquivo; lista vazia se não houver linhas válidas.
    """
    texto = None
    for encoding in ('utf-8-sig', 'latin-1'):
        try:
            with open(caminho, newline='', encoding=encoding) as f:
                texto = f.read()
            break
        except UnicodeDecodeError:
            continue
    if texto is None:
        return None

    linhas = [l for l in csv.reader(texto.splitlines()) if l and any(c.strip() for c in l)]
    if not linhas:
        return []

    # A 1ª célula numérica pura indica arquivo sem cabeçalho (tag na 1ª coluna)
    primeira = [c.strip().lower() for c in linhas[0]]
    if primeira[0].isdigit():
        mapa = (0, 1, 2)  # ordem antiga: tag, id_aparelho, local
        inicio = 0
    else:
        idx_tag = idx_id = idx_local = None
        for i, n in enumerate(primeira):
            if 'central_monitoramento' in n or n in ('tag', 'id_central', 'id_tag'):
                idx_tag = i
            elif 'aparelho' in n or 'equipamento' in n or 'evaporadora' in n:
                idx_id = i
            elif n in ('local', 'sala', 'ambiente', 'descricao'):
                idx_local = i
        if None in (idx_tag, idx_id, idx_local):
            # Heurística: exatamente uma célula numérica (0-63) por linha define a tag
            resultado = []
            for linha in linhas:
                pos = [i for i, c in enumerate(linha)
                       if (c or '').strip().isdigit() and 0 <= int(c) < TAGS_POR_CENTRAL]
                if len(pos) == 1 and len(linha) >= 3:
                    t = pos[0]
                    outros = [i for i in range(len(linha)) if i != t]
                    if linha[outros[0]].strip():
                        resultado.append((int(linha[t]), linha[outros[0]].strip(),
                                          linha[outros[1]].strip()))
            if resultado:
                logger.warning('CSV %s: cabeçalho não reconhecido — %d linha(s) importadas por '
                               'heurística; confira o cadastro', caminho, len(resultado))
            else:
                logger.warning('CSV %s: cabeçalho não reconhecido (%s) — use as colunas '
                               'id_central_monitoramento, id_aparelho, local', caminho, linhas[0])
            return resultado
        mapa = (idx_tag, idx_id, idx_local)
        inicio = 1

    resultado = []
    for linha in linhas[inicio:]:
        try:
            bruto_tag, id_fisico, local = linha[mapa[0]], linha[mapa[1]], linha[mapa[2]]
        except IndexError:
            continue
        bruto_tag = (bruto_tag or '').strip()
        if not bruto_tag.isdigit():
            continue
        tag = int(bruto_tag)
        if not (0 <= tag < TAGS_POR_CENTRAL):
            logger.warning('tag fora do intervalo 0-63 ignorada: %s', bruto_tag)
            continue
        resultado.append((tag, (id_fisico or '').strip(), (local or '').strip()))
    return resultado


def importar_csv_aparelhos(conn, central):
    """Importa/atualiza o cadastro de evaporadoras a partir do CSV de identificação.

    Formato (colunas em qualquer ordem, com cabeçalho):
        id_aparelho,local,id_central_monitoramento
        UE39 S101,SALA 101,00
        UE41 S102,SALA 102 AUDITÓRIO,02
    A coluna id_central_monitoramento é a tag aN no status.xml da central (0-63).

    Caminhos relativos são resolvidos a partir da pasta do próprio monitor_ar.py
    (BASE_DIR), funcionando igual no terminal e via systemd.

    Não sobrescreve potencia_kw — atualiza apenas identificação (nome/sala/ala).
    """
    caminho = (central.get('csv') or '').strip()
    nome = central.get('nome', f"central_{central['id']}")
    if not caminho:
        logger.info('[%s] sem CSV de cadastro configurado — importação pulada', nome)
        return

    if not os.path.isabs(caminho):
        caminho = os.path.join(BASE_DIR, caminho)

    if not os.path.exists(caminho):
        logger.warning('[%s] CSV não encontrado: %s — importação pulada', nome, caminho)
        return

    linhas = ler_csv_aparelhos(caminho)
    if linhas is None:
        logger.warning('[%s] não foi possível decodificar o CSV: %s', nome, caminho)
        return
    if not linhas:
        logger.warning('[%s] nenhuma linha válida no CSV: %s (confira o cabeçalho/colunas)',
                       nome, caminho)
        return

    novos = atualizados = 0
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT id_central FROM aparelhos')
            existentes = {r[0] for r in cur.fetchall()}

            for tag, id_fisico, local in linhas:
                gid = id_global(central['id'], tag)
                cur.execute('''
                    INSERT INTO aparelhos (id_central, id_aparelho, local, ala)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (id_central) DO UPDATE
                    SET id_aparelho = EXCLUDED.id_aparelho,
                        local       = EXCLUDED.local,
                        ala         = EXCLUDED.ala
                ''', (gid, id_fisico, local, nome))

                if gid in existentes:
                    atualizados += 1
                else:
                    novos += 1
                    existentes.add(gid)
        conn.commit()
        logger.info('[%s] cadastro do CSV: %d novo(s), %d atualizado(s) — %s',
                    nome, novos, atualizados, caminho)
    except psycopg2.Error as e:
        conn.rollback()
        logger.error('[%s] erro ao importar CSV %s: %s', nome, caminho, e)


def init_db(conn):
    """Cria as tabelas (se não existirem), a coluna 'ala' e importa os CSVs."""
    with conn.cursor() as cur:
        cur.execute('''
            CREATE TABLE IF NOT EXISTS aparelhos (
                id_central   INTEGER PRIMARY KEY,
                id_aparelho  VARCHAR(50),
                local        VARCHAR(150),
                ala          VARCHAR(10),
                potencia_kw  NUMERIC(5,2)
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS historico_status (
                id            SERIAL PRIMARY KEY,
                "timestamp"   TIMESTAMP NOT NULL DEFAULT NOW(),
                id_central    INTEGER REFERENCES aparelhos(id_central),
                hex_payload   VARCHAR(30),
                is_on         SMALLINT,
                temp_ambiente SMALLINT,
                temp_setada   SMALLINT
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS parametros (
                chave VARCHAR(50) PRIMARY KEY,
                valor VARCHAR(100)
            )
        ''')
        # Migração multi-central: instalações antigas não têm a coluna 'ala'
        cur.execute("ALTER TABLE aparelhos ADD COLUMN IF NOT EXISTS ala VARCHAR(10)")
        cur.execute(
            'CREATE INDEX IF NOT EXISTS idx_hist_central_time '
            'ON historico_status(id_central, "timestamp")'
        )
        cur.execute(
            "INSERT INTO parametros (chave, valor) VALUES ('tarifa_kwh', '0.85') "
            'ON CONFLICT (chave) DO NOTHING'
        )
    conn.commit()
    logger.info('Banco de dados verificado (tabelas, coluna ala, índices).')

    for central in CENTRAIS:
        importar_csv_aparelhos(conn, central)


# ---------------------------------------------------------------------------
# Coleta
# ---------------------------------------------------------------------------
def coletar_central(central):
    """Baixa o status.xml de uma central e decodifica todas as evaporadoras.

    Retorna dict {numero_tag: {'hex': str, 'is_on': int, 'temp_ambiente': int,
    'temp_setada': int}} ou None em caso de falha (rede/XML).
    """
    url = central['url']
    nome = central.get('nome', f"central_{central['id']}")
    try:
        resp = requests.get(url, timeout=TIMEOUT_HTTP)
        resp.raise_for_status()
        resp.encoding = 'gb2312'  # encoding usado pelo CCM-15
        root = ET.fromstring(resp.text)
    except requests.RequestException as e:
        logger.error('[%s] falha ao acessar %s: %s', nome, url, e)
        return None
    except ET.ParseError as e:
        logger.error('[%s] XML inválido de %s: %s', nome, url, e)
        return None

    leituras = {}
    for elem in root.iter():
        m = re.fullmatch(r'a(\d{1,2})', elem.tag or '')
        if not m:
            continue
        numero = int(m.group(1))
        if numero >= TAGS_POR_CENTRAL:
            continue
        bruto = (elem.text or elem.get('value') or '').strip().rstrip(',')
        if not bruto:
            continue
        dados = parse_ac_payload(bruto)
        if dados is None:
            continue
        leituras[numero] = {'hex': bruto, **dados}

    logger.debug('[%s] %d evaporadora(s) lida(s)', nome, len(leituras))
    return leituras


def collect_data(conn, cache, stats=None):
    """Executa um ciclo completo de coleta de TODAS as centrais e grava no banco.

    cache: {id_global: (assinatura_do_estado, timestamp_da_ultima_gravacao)}
      - grava somente quando is_on/temp_ambiente/temp_setada mudam;
      - keep-alive: força gravação a cada FORCAR_GRAVACAO_MINUTOS mesmo sem mudança
        (garante continuidade do histórico em períodos de estado constante).

    stats: dict acumulador opcional para o resumo periódico (heartbeat).
    """
    if stats is None:
        stats = {}
    agora = time.time()
    forcar_apos = FORCAR_GRAVACAO_MINUTOS * 60

    ids_cadastrados = carregar_ids_cadastrados(conn)
    registros = []

    for central in CENTRAIS:
        leituras = coletar_central(central)
        if leituras is None:
            stats['falhas_central'] = stats.get('falhas_central', 0) + 1
            continue  # isolamento de falha: segue para a próxima central
        stats['leituras'] = stats.get('leituras', 0) + len(leituras)

        for numero, dados in leituras.items():
            gid = id_global(central['id'], numero)

            # Proteção de FK: só grava aparelho existente em 'aparelhos'
            if gid not in ids_cadastrados:
                logger.warning(
                    '[%s] aparelho %d (tag a%d) sem cadastro em "aparelhos" — '
                    'registro ignorado (revise o CSV da central)',
                    central.get('nome'), gid, numero,
                )
                continue

            assinatura = f"{dados['is_on']}|{dados['temp_ambiente']}|{dados['temp_setada']}"
            anterior = cache.get(gid)
            if anterior and anterior[0] == assinatura and (agora - anterior[1]) < forcar_apos:
                continue  # nada mudou desde a última gravação

            registros.append(
                (gid, dados['hex'], dados['is_on'],
                 dados['temp_ambiente'], dados['temp_setada'])
            )
            cache[gid] = (assinatura, agora)

    if not registros:
        logger.debug('Nenhuma mudança de estado neste ciclo.')
        return

    try:
        with conn.cursor() as cur:
            execute_batch(cur, '''
                INSERT INTO historico_status
                    ("timestamp", id_central, hex_payload, is_on, temp_ambiente, temp_setada)
                VALUES
                    (NOW(), %s, %s, %s, %s, %s)
            ''', registros, page_size=500)
        conn.commit()
        stats['gravacoes'] = stats.get('gravacoes', 0) + len(registros)
        logger.info('%d registro(s) gravado(s) no banco.', len(registros))
    except psycopg2.Error as e:
        logger.error('Erro ao gravar no banco: %s', e)
        try:
            conn.rollback()
        except Exception:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------------
def main():
    # 1) Validar ANTES de tocar no banco
    validar_config()

    # 2) Preparar banco (tabelas, coluna 'ala', índices, cadastro dos CSVs)
    conn = get_connection()
    init_db(conn)

    # 3) Loop de coleta
    logger.info('Coleta iniciada. Ctrl+C para encerrar.')
    cache = {}
    stats = {}
    ultimo_resumo = time.time()
    try:
        while True:
            inicio = time.time()
            try:
                conn = garantir_conexao(conn)
                collect_data(conn, cache, stats)
                stats['ciclos_ok'] = stats.get('ciclos_ok', 0) + 1
            except KeyboardInterrupt:
                raise
            except Exception:
                stats['ciclos_erro'] = stats.get('ciclos_erro', 0) + 1
                logger.exception('Erro inesperado no ciclo de coleta')

            # Resumo periódico: visibilidade sem ruído a cada minuto
            if HEARTBEAT_MINUTOS > 0 and (time.time() - ultimo_resumo) >= HEARTBEAT_MINUTOS * 60:
                logger.info(
                    'Resumo (últimos %d min): %d ciclo(s) OK | %d com erro | '
                    '%d falha(s) de central | %d leitura(s) | %d gravação(ões)',
                    HEARTBEAT_MINUTOS,
                    stats.get('ciclos_ok', 0), stats.get('ciclos_erro', 0),
                    stats.get('falhas_central', 0),
                    stats.get('leituras', 0), stats.get('gravacoes', 0),
                )
                stats = {}
                ultimo_resumo = time.time()

            restante = INTERVALO_SEGUNDOS - (time.time() - inicio)
            if restante > 0:
                time.sleep(restante)
    except KeyboardInterrupt:
        logger.info('Encerrado pelo usuário.')
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == '__main__':
    main()
