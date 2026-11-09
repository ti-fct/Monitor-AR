-- ============================================================================
-- Ajuste de fuso horário — FCT/UFG — Monitor de Ar-Condicionado CCM-15
--
-- Diagnóstico (09/nov/2026, ~11:40 no relógio de São Paulo):
--     SELECT NOW();  ->  2026-11-09 14:40:47.811008+00
--   O sufixo "+00" mostra que a sessão/servidor PostgreSQL está em UTC.
--   14:40 UTC == 11:40 de Brasília (America/Sao_Paulo, UTC-3 fixo — o Brasil
--   não tem mais horário de verão desde 2019), ou seja: a hora ABSOLUTA está
--   correta, mas os timestamps eram gravados com o RÓTULO em UTC.
--
--   A coluna historico_status."timestamp" é TIMESTAMP (sem fuso): o NOW()
--   gravado pelo coletor era convertido para o fuso da sessão (UTC), ficando
--   3h à frente da hora local. Este script corrige em 3 passos.
--
-- ONDE RODAR: psql no banco do monitor, ex.:
--     psql -U suporte -d ar_condicionado_db -f ajuste_fuso_horario.sql
--   (ou cole os passos um a um no pgAdmin/DBeaver/Metabase SQL editor)
-- ============================================================================


-- ============================================================================
-- PASSO 1 — Diagnóstico (opcional, só para confirmar o cenário)
-- ============================================================================
SHOW timezone;

SELECT NOW()                                      AS agora_sessao,
       NOW() AT TIME ZONE 'UTC'                   AS agora_utc,
       NOW() AT TIME ZONE 'America/Sao_Paulo'     AS agora_sp,
       (SELECT MAX("timestamp") FROM historico_status) AS ultima_leitura;
-- Se "agora_sessao" estiver ~3h à frente do seu relógio e "ultima_leitura"
-- também, o cenário está confirmado. "agora_sp" deve bater com o relógio.


-- ============================================================================
-- PASSO 2 — Tornar America/Sao_Paulo o fuso PADRÃO do banco (recomendado)
-- ============================================================================
-- Faz psql, pgAdmin, DBeaver e novas conexões do Metabase exibirem/gravarem em
-- hora local por padrão. Não afeta dados já gravados; é cumulativo com o
-- DB_OPTIONS que o monitor_ar.py agora envia na própria conexão.
ALTER DATABASE ar_condicionado_db SET timezone TO 'America/Sao_Paulo';

-- Aplica já na sessão atual (equivale a reconectar):
SET timezone TO 'America/Sao_Paulo';

-- Verificação:
SHOW timezone;
SELECT NOW() AS agora;   -- deve aparecer, ex.: 2026-11-09 11:40:00-03


-- ============================================================================
-- PASSO 3 — Converter o histórico existente de UTC para hora local
--           >>> RODAR UMA ÚNICA VEZ <<<
-- ============================================================================
-- Mecânica da conversão (timestamp sem fuso -> timestamp sem fuso):
--   "timestamp" AT TIME ZONE 'UTC'             interpreta o valor gravado como UTC
--   ... AT TIME ZONE 'America/Sao_Paulo'       converte para o relógio de Brasília
--   Ex.: 14:40 (gravado como UTC) -> 11:40 (hora local real da leitura)
--
-- Se o histórico atual for só teste e puder ser descartado, use no lugar:
--     TRUNCATE historico_status;   -- e pule este passo (coletor recomeça do zero)

BEGIN;

-- 3a. Antes: amplitude e volume esperados da conversão
SELECT COUNT(*)                        AS registros_a_converter,
       MIN("timestamp")                AS primeiro_utc,
       MAX("timestamp")                AS ultimo_utc
FROM historico_status;

-- 3b. Conversão
UPDATE historico_status
SET    "timestamp" = "timestamp" AT TIME ZONE 'UTC' AT TIME ZONE 'America/Sao_Paulo';

-- 3c. Conferir DENTRO da transação (ainda sem commit):
--     "ultimo_local" deve ser ~3h MENOR que "ultimo_utc" do passo 3a e
--     próximo da hora do relógio (menos a pausa do coletor, se houver).
SELECT COUNT(*)         AS total_convertido,
       MIN("timestamp") AS primeiro_local,
       MAX("timestamp") AS ultimo_local
FROM historico_status;

COMMIT;    -- se algo parecer estranho: ROLLBACK; e me avise antes de prosseguir


-- ============================================================================
-- PASSO 4 — Verificação final
-- ============================================================================
-- Reinicie o coletor (systemctl restart monitor-ar), aguarde 1-2 ciclos e rode:
SELECT NOW()                                            AS agora_sp,
       (NOW() AT TIME ZONE 'America/Sao_Paulo')         AS agora_local_naive,
       MAX("timestamp")                                 AS ultima_leitura
FROM historico_status;
-- "ultima_leitura" deve estar a poucos minutos de "agora_local_naive"
-- (o keep-alive grava a cada 60 min; mudança de estado grava em ~1 min).

-- E na view de janelas (v3): os rótulos de hora devem bater com o relógio
-- local, sem deslocamento de 3h.
