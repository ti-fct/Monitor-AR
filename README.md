# Monitor de Ar-Condicionado CCM-15 — Midea VRF

Sistema de monitoramento e análise de consumo energético dos sistemas de ar-condicionado central (VRF) da FCT/UFG, baseado nos controladores centrais **CCM-15 da Midea**.

O coletor consulta periodicamente o `status.xml` de cada controlador CCM, decodifica o payload hexadecimal de cada evaporadora (estado on/off, temperatura ambiente e temperatura setada) e grava no **PostgreSQL**. A camada analítica é composta por views SQL consumidas por dashboards no **Metabase**, que apoiam estudos de eficiência energética em prédios públicos.

## Arquitetura

```
CCM-15 Ala A (192.168.x.x) ──┐
                              ├──► monitor_ar.py ──► PostgreSQL ──► Metabase
CCM-15 Ala B (192.168.x.x) ──┘     (coleta 60s)      (historico)    (dashboards)
```

## Funcionalidades

- Coleta **multi-central**: Ala A e Ala B definidas por configuração, sem alterar código (extensível a novas alas)
- Gravação **por mudança de estado**, com registro de manutenção (keep-alive) a cada 60 minutos
- Decodificação do payload hexadecimal do CCM-15 (modo, temperatura ambiente, temperatura setada)
- Identificador **global de aparelho** (`id_central * 100 + numero`), evitando colisão de numeração entre centrais
- Isolamento de falhas: uma central fora do ar não interrompe a coleta das demais
- Views SQL prontas: eventos liga/desliga, sessões de funcionamento, consumo estimado, perfil horário e graus-hora
- Integração com **Metabase** (dashboards com filtros de período, ala e sala)
- Configuração segura via `.env` (python-dotenv), com `.env.example` versionado

## Requisitos

- Python 3.9+
- PostgreSQL 12+
- Metabase (opcional, para os dashboards)
- Acesso de rede aos controladores CCM (porta 80)

## Instalação

```bash
git clone https://github.com/<usuario>/monitor-ar-ccm15.git
cd monitor-ar-ccm15

python3 -m venv venv
source venv/bin/activate        # Linux
# venv\Scripts\activate         # Windows

pip install -r requirements.txt
```

### Banco de dados

```sql
CREATE USER suporte WITH PASSWORD 'senha_forte';
CREATE DATABASE ar_condicionado_db OWNER suporte;
```

As tabelas (`aparelhos`, `historico_status`, `parametros`) e os índices são criados automaticamente na primeira execução do coletor. A camada analítica (views) deve ser aplicada a partir dos arquivos em `sql/`.

## Configuração

Copie o template e preencha com os valores reais:

```bash
cp .env.example .env
```

| Variável | Descrição |
|---|---|
| `DB_NAME` / `DB_USER` / `DB_PASSWORD` / `DB_HOST` / `DB_PORT` | Credenciais do PostgreSQL |
| `INTERVALO_SEGUNDOS` | Intervalo entre ciclos de coleta (padrão: 60) |
| `FORCAR_GRAVACAO_MINUTOS` | Grava mesmo sem mudança de estado a cada N minutos (padrão: 60) |
| `CENTRAIS_JSON` | Lista JSON com as centrais CCM (id, nome, url, csv) |
| `LOG_LEVEL` | Verbosidade: `INFO` (padrão) ou `DEBUG` para detalhes por ciclo de coleta |
| `HEARTBEAT_MINUTOS` | Intervalo do resumo operacional no log (padrão: 15; `0` desativa) |

Exemplo de `CENTRAIS_JSON`:

```json
[
  {"id": 1, "nome": "ALA_A", "url": "http://192.168.x.x/status.xml", "csv": "Identificação do sistema de ar condicionado - ALA_A.csv"},
  {"id": 2, "nome": "ALA_B", "url": "http://192.168.x.x/status.xml", "csv": "Identificação do sistema de ar condicionado - ALA_B.csv"}
]
```

> **Atenção:** o `id` da central compõe o identificador global do aparelho (ver Modelo de dados). Não altere depois que houver dados no banco.

### CSV de mapeamento das evaporadoras

Cada central possui um CSV de identificação (formato exportado pela ferramenta Midea), com as colunas em **qualquer ordem** — o coletor reconhece o cabeçalho automaticamente:

| id_aparelho | local | id_central_monitoramento |
|---|---|---|
| UE39 S101 | SALA 101 | 00 |
| UE40 S101 | SALA 101 | 01 |
| UE41 S102 | SALA 102 AUDITÓRIO | 02 |

- `id_central_monitoramento`: número da tag `aN` no `status.xml` daquela central (0–63, zeros à esquerda aceitos) — **confira no XML real antes de montar o CSV**
- `id_aparelho`: identificação física da evaporadora
- `local`: sala/ambiente onde o aparelho está instalado

Caminhos relativos no `.env` são resolvidos a partir da pasta do próprio `monitor_ar.py`, funcionando igual no terminal e via systemd. O import atualiza nome/sala/ala e **não sobrescreve** `potencia_kw`.

### Tarifa de energia

A view de custo utiliza a tabela `parametros`:

```sql
INSERT INTO parametros (chave, valor) VALUES ('tarifa_kwh', 0.85)
ON CONFLICT (chave) DO UPDATE SET valor = EXCLUDED.valor;
```

Use a tarifa real (R$/kWh) da fatura da distribuidora — idealmente separando posto ponta e fora de ponta.

## Decodificação do payload do `status.xml`

Cada tag `aN` do XML contém 7 bytes em hexadecimal (14 caracteres), seguidos de vírgula. Campos decodificados pelo coletor:

| Byte | Conteúdo |
|---|---|
| 0 | bit 0: unidade de temperatura (0 = °C, 1 = °F) |
| 3 | bits 2–4: modo (0=Cool, 1=Heat, 2/3=Fan, 4=Off, 5=Auto, 6=Offline, 7=Falha); bits 5–7: ventilador (0=Auto, 2=Low, 3=Med, 4=High) |
| 4 | bit 0: eletroaquecedor; bit 1: swing; **bits 3–7: temperatura setada** (°C = valor direto 17–30; °F = valor + 62) |
| 6 | **temperatura ambiente** (byte com sinal / complemento de dois) |

Os demais bytes carregam configurações de bloqueio (modo/ventilador/temperatura/remoto) e código de erro, reservados para uso futuro.

## Execução

```bash
python monitor_ar.py
```

### Execução como serviço (systemd)

```ini
# /etc/systemd/system/monitor-ar.service
[Unit]
Description=Monitor de Ar-Condicionado CCM-15
After=network.target postgresql.service

[Service]
WorkingDirectory=/opt/monitor-ar-ccm15
ExecStart=/opt/monitor-ar-ccm15/venv/bin/python monitor_ar.py
Restart=always
RestartSec=30
User=monitor

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now monitor-ar
journalctl -u monitor-ar -f
```

## Modelo de dados

### `aparelhos`

| Coluna | Tipo | Descrição |
|---|---|---|
| `id_central` | INT PK | Identificador **global** do aparelho |
| `id_aparelho` | VARCHAR(50) | Identificação física (ex.: UE02-T03) |
| `local` | VARCHAR(150) | Sala/ambiente |
| `ala` | VARCHAR(10) | Central à qual pertence (ALA_A / ALA_B) |
| `potencia_kw` | NUMERIC(5,2) | Potência elétrica estimada (para cálculo de consumo) |

### `historico_status`

| Coluna | Tipo | Descrição |
|---|---|---|
| `id` | SERIAL PK | Sequência |
| `timestamp` | TIMESTAMP | Momento da leitura |
| `id_central` | INT FK | Referência a `aparelhos` |
| `hex_payload` | VARCHAR(30) | Payload bruto (auditoria/reprocessamento) |
| `is_on` | INT | 1 = ligado, 0 = desligado/offline |
| `temp_ambiente` | INT | Temperatura ambiente (°C) |
| `temp_setada` | INT | Temperatura setada (°C) |

### Identificador global de aparelho

Como cada CCM numera suas evaporadoras de 0 a 63, o identificador global evita colisão entre centrais:

```
id_global = id_central * 100 + numero_da_tag
```

| Central | id | Faixa de IDs globais |
|---|---|---|
| ALA_A | 1 | 100–163 |
| ALA_B | 2 | 200–263 |

## Camada analítica (views SQL)

Aplicar os arquivos da pasta `sql/` no banco. Principais views:

| View | Finalidade |
|---|---|
| `v_eventos_onoff` | Horários exatos de liga/desliga por aparelho e sala |
| `v_sessoes_funcionamento` | Sessões ligado: início, fim, horas, temperaturas do período |
| `v_consumo_diario` | kWh e custo estimados por aparelho/dia (potência × horas) |
| `v_ocupacao_por_hora` | Perfil de uso: dia da semana × hora |
| `v_degree_hours_diario` | Graus-hora acima de 24 °C (métrica de carga térmica) |
| `v_aparelhos_ligados_por_dia` | Quantidade de aparelhos ligados por dia e pico simultâneo |
| `v_aparelhos_ligados_por_sala_dia` | Mesma visão agrupada por sala |

## Metabase

1. **Admin → Databases → Sync database schema** (após criar as views).
2. Marcar o campo `sala` como *Category* com lista de valores habilitada (para filtros dropdown).
3. Dashboards sugeridos:
   - **Visão Geral**: KPIs (ligados agora, % ligação, custo hoje), atividade por 30 min, linha 7 dias, tabela de status atual;
   - **Salas**: horas por sala, subutilização (pct de aparelhos ligados);
   - **Eventos/Consumo**: log de eventos e consumo estimado.
4. Filtros de dashboard: **Período**, **Ala**, **Sala** — cards em SQL nativo devem usar *Field Filters* para aceitar os filtros.
5. Recursos recomendados: formatação condicional (LIGADO/DESLIGADO), auto-refresh de 1 minuto e *click behavior* na tabela de status.

## Segurança

- O arquivo `.env` **nunca** deve ser versionado (já incluído no `.gitignore`).
- Utilize senha forte no PostgreSQL e restrinja o `pg_hba.conf` à rede local.
- Em caso de exposição acidental de credenciais, troque a senha imediatamente (`ALTER USER ... WITH PASSWORD ...`).

## Estrutura do projeto

```
monitor-ar-ccm15/
├── monitor_ar.py          # Coletor (polling, decodificação, gravação)
├── requirements.txt       # Dependências Python
├── .env.example           # Template de configuração
├── .gitignore
├── sql/
│   ├── schema.sql         # (opcional) criação manual das tabelas
│   └── views.sql          # Camada analítica
├── Identificação ... ALA_A.csv
├── Identificação ... ALA_B.csv
└── README.md
```

## Roadmap

- [ ] Armazenar modo de operação, velocidade do ventilador e código de erro (bytes 3 e 2 do payload)
- [ ] Calibrar o consumo estimado com as faturas reais de energia
- [ ] Registrar posto horário (ponta/fora de ponta) no cálculo de custo
- [ ] Integração com sensores de ocupação
- [ ] View comparativa entre alas (consumo e horas lado a lado)


## Licença

Distribuído sob a licença <MIT / GPL-3.0 / uso interno>. Consulte o arquivo `LICENSE` para detalhes.