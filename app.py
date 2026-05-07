from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import gspread
from google.oauth2.service_account import Credentials
import os
import json
from datetime import datetime
import pytz

app = Flask(__name__, static_folder='static')
CORS(app)

SHEET_ID = '1na0NwEEN8khztM53d_c33yt_Wgnrc4At2akqYq2g5zA'
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
BR_TZ = pytz.timezone('America/Sao_Paulo')

EQUIPE = ['Gustavo Henrique', 'Diego Demétrio', 'Felício Lemos', "Rafael Sant'Anna"]
STATUS_LIST = [
    'FILA','CONSULTA DE DADOS','CONSULTA TÉCNICA PROCESSADORA',
    'JURÍDICO','COMITÊ EXECUTIVO','PENDÊNCIA COMITÊ',
    'CREDENCIAMENTO','DOCS ENVIADOS','DOCS EM ANÁLISE',
    'ASSINATURA','RUBRICA','CONTRATO PROCESSADORA',
    'CREDENCIADO','IMPEDIDO','CONFLITO IF'
]
ETAPAS_ALERTA = [
    'FILA','CONSULTA DE DADOS','CONSULTA TÉCNICA PROCESSADORA',
    'JURÍDICO','COMITÊ EXECUTIVO','PENDÊNCIA COMITÊ',
    'CREDENCIAMENTO','DOCS ENVIADOS','DOCS EM ANÁLISE',
    'ASSINATURA','RUBRICA','CONTRATO PROCESSADORA'
]
DIAS_ALERTA = 15

def get_client():
    creds_json = os.environ.get('GOOGLE_CREDS')
    if not creds_json:
        raise Exception('GOOGLE_CREDS não configurado')
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)

def get_sheet(name):
    client = get_client()
    sh = client.open_by_key(SHEET_ID)
    return sh.worksheet(name)

@app.route('/')
def index():
    return send_from_directory('.', 'index_v10.html')

@app.route('/api/dados')
def get_dados():
    try:
        master = get_sheet('Master')
        corbans_ws = get_sheet('Corbans')

        m_vals = master.get_all_values()
        m_rows = [r for r in m_vals[2:] if r[0] and r[0].strip()]

        municipios = []
        for r in m_rows:
            def safe(i): return r[i].strip() if i < len(r) else ''
            def num(i):
                try: return float(safe(i).replace('.','').replace(',','.')) if safe(i) else None
                except: return None
            municipios.append({
                'nome': safe(0), 'tipo': safe(1),
                'status': safe(2).upper(), 'capag': safe(3),
                'ifAdm': safe(4), 'margem': num(5),
                'colab': num(6), 'pot': num(7), 'populacao': num(8),
                'processadora': safe(9), 'api': safe(10),
                'integ': safe(11), 'reav': safe(12),
                'contratados': safe(13), 'parceiro': safe(14),
                'obs': safe(21) if len(r) > 21 else '',
            })

        c_vals = corbans_ws.get_all_values()
        c_rows = [r for r in c_vals[2:] if r[0] and r[0].strip()]
        corbans_list = []
        for r in c_rows:
            def sc(i): return r[i].strip() if i < len(r) else ''
            corbans_list.append({
                'nome': sc(0), 'status': sc(1),
                'pracas': [sc(i) for i in range(2,7) if sc(i)]
            })

        alertas = get_alertas(municipios)

        now_br = datetime.now(BR_TZ).strftime('%d/%m/%Y %H:%M')
        return jsonify({
            'municipios': municipios,
            'corbans': corbans_list,
            'equipe': EQUIPE,
            'statusList': STATUS_LIST,
            'alertas': alertas,
            'atualizado': now_br
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def get_alertas(municipios):
    try:
        hist = get_sheet('Histórico')
        h_vals = hist.get_all_values()
        ultima = {}
        for r in h_vals[1:]:
            nome = r[1].strip() if len(r) > 1 else ''
            if not nome: continue
            try:
                dt = datetime.strptime(r[0].strip()[:16], '%d/%m/%Y %H:%M')
                if nome not in ultima or dt > ultima[nome]:
                    ultima[nome] = dt
            except: pass

        hoje = datetime.now(BR_TZ).replace(tzinfo=None)
        alertas = []
        for m in municipios:
            if m['status'] not in ETAPAS_ALERTA: continue
            ult = ultima.get(m['nome'])
            if ult:
                dias = (hoje - ult).days
            else:
                dias = 0
            if dias >= DIAS_ALERTA:
                alertas.append({
                    'nome': m['nome'], 'status': m['status'],
                    'diasParado': dias,
                    'ultimaAtualizacao': ult.strftime('%d/%m/%Y') if ult else 'Sem registro'
                })
        alertas.sort(key=lambda x: x['diasParado'], reverse=True)
        return alertas
    except:
        return []

@app.route('/api/historico')
def get_historico():
    nome = request.args.get('nome', '')
    try:
        hist = get_sheet('Histórico')
        vals = hist.get_all_values()
        rows = []
        for r in vals[1:]:
            if len(r) > 1 and r[1].strip() == nome.strip():
                rows.append({
                    'data': r[0], 'convenio': r[1],
                    'statusAnterior': r[2], 'statusNovo': r[3],
                    'observacao': r[4], 'responsavel': r[5] if len(r) > 5 else ''
                })
        rows.reverse()
        return jsonify(rows)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/evoluir', methods=['POST'])
def evoluir():
    try:
        dados = request.json
        master = get_sheet('Master')
        m_vals = master.get_all_values()

        linha = -1
        status_ant = ''
        for i, r in enumerate(m_vals[2:], start=3):
            if r[0].strip() == dados['nome'].strip():
                linha = i
                status_ant = r[2].strip()
                break

        if linha == -1:
            return jsonify({'ok': False, 'msg': 'Convênio não encontrado.'})

        master.update_cell(linha, 3, dados['statusNovo'])
        master.update_cell(linha, 22, dados['obs'])

        hist = get_sheet('Histórico')
        agora = datetime.now(BR_TZ).strftime('%d/%m/%Y %H:%M')
        hist.append_row([agora, dados['nome'], status_ant, dados['statusNovo'], dados['obs'], dados['responsavel']])

        return jsonify({'ok': True, 'msg': 'Status atualizado com sucesso!'})
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)})

@app.route('/api/adicionar', methods=['POST'])
def adicionar():
    try:
        dados = request.json
        master = get_sheet('Master')
        m_vals = master.get_all_values()

        for r in m_vals[2:]:
            if r[0].strip().upper() == dados['nome'].upper():
                return jsonify({'ok': False, 'msg': 'Convênio já existe na planilha.'})

        nova = [
            dados.get('nome',''), dados.get('tipo',''), dados.get('status',''),
            dados.get('capag',''), dados.get('ifAdm',''), dados.get('margem',''),
            dados.get('colab',''), dados.get('pot',''), dados.get('pop',''),
            dados.get('proc',''), dados.get('api',''), dados.get('integ',''),
            dados.get('reav',''), dados.get('contrat',''), dados.get('parceiro',''),
            '','','','','','', dados.get('obs','')
        ]
        master.append_row(nova)

        hist = get_sheet('Histórico')
        agora = datetime.now(BR_TZ).strftime('%d/%m/%Y %H:%M')
        hist.append_row([agora, dados['nome'], '—', dados['status'],
                        'Adicionado via dashboard. ' + dados.get('obs',''),
                        dados.get('responsavel','')])

        return jsonify({'ok': True, 'msg': 'Convênio adicionado com sucesso!'})
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
