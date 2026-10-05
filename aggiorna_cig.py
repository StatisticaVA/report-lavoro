#!/usr/bin/env python3
"""Scarica dall'INPS le ore autorizzate di cassa integrazione e salva in dati/ solo quello che serve alla sezione «Cassa integrazione» della pagina «Lavoro».

Lo usa l'aggiornamento automatico su GitHub (.github/workflows/aggiorna-cig.yml, ogni giorno dal 2 al 12 del mese), ma si
può lanciare anche a mano:  python3 aggiorna_cig.py        Solo libreria standard di Python (niente da installare).

Perché serve: il servizio dell'INPS (Osservatori statistici) non permette a una pagina web di leggere i dati da sola
(niente intestazioni CORS), quindi li scarica questo programma e la pagina li legge dal proprio repository.

Fonte: Osservatorio INPS «Cassa integrazione guadagni e fondi di solidarietà – Ore autorizzate», classificazione secondo il
codice statistico contributivo Inps: serie mensili 2023→oggi (osservatorio 512) e 2009–2022 (osservatorio 304, «old», fermo).
Le ore sono flussi mensili. Per i territori e i settori si scarica direttamente il CUMULATO da gennaio a fine trimestre
(l'INPS rifiuta, per riservatezza, alcune tavole con i singoli mesi; con i periodi cumulati le richieste sono accettate).

File prodotti in dati/ (CSV, una riga per valore):
  cig_territori.csv        anno,periodo,tipo,territorio,ore       12 province lombarde + Lombardia (somma) + Italia;
                           periodo = trimestre (1…4), ore CUMULATE da gennaio a fine trimestre; tipo = Ordinaria / Straordinaria / Deroga
  cig_classi.csv           anno,periodo,tipo,classe,ore           Varese per classe di attività economica (codice statistico contributivo)
  cig_varese_mensile.csv   anno,mese,tipo,ore                      Varese, ore di ogni singolo mese dal 2009 (per le tabelle nel tempo)
Il «Totale» si calcola nella pagina (somma dei tre tipi). Si salvano solo i periodi già pubblicati dall'INPS.

Se per una parte qualcosa non va (INPS non risponde, tavola cambiata, numeri incoerenti) i file NON vengono toccati e il
problema viene scritto nel file di esito (--esito): il workflow apre una segnalazione chiara (e-mail) con segnala_problemi.py.
Se l'INPS non ha pubblicato dati nuovi non è un problema. Con PROVA_ERRORE=true si simula un problema (prova dell'e-mail).
"""
import argparse
import csv
import gzip
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

from aggiorna_dati import CARTELLA, Problema, testo_csv

API = "https://servizi2.inps.it/servizi/osservatoristatistici/api/"
OSS_NUOVO, OSS_VECCHIO = 512, 304            # serie storiche mensili con classificazione Inps: 2023→oggi e 2009–2022
NOME_OSS = "Classificazione secondo il codice statistico contributivo Inps"
MESI = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno", "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]
TIPI = ["Ordinaria", "Straordinaria", "Deroga"]
PROVINCE = ["Milano", "Bergamo", "Brescia", "Como", "Cremona", "Lecco", "Lodi", "Mantova", "Monza e della Brianza", "Pavia", "Sondrio", "Varese"]
CLASSI = ["Attivita' economiche connesse con l'agricoltura", "Estrazione minerali metalliferi e non", "Legno", "Alimentari", "Metallurgiche",
          "Meccaniche", "Tessili", "Abbigliamento", "Chimica, petrolchimica, gomma e materie plastiche", "Pelli, cuoio e calzature",
          "Lavorazione minerali non metalliferi", "Carta, stampa ed editoria", "Installazione impianti per l'edilizia",
          "Energia elettrica, gas e acqua", "Trasporti e comunicazioni", "Tabacchicoltura", "Servizi", "Varie", "Commercio all'ingrosso",
          "Commercio al minuto",
          "Attivita' varie (Professionisti, artisti, scuole e istituti privati di istruzione, istituti di vigilanza, case di cura private",
          "Intermediari (Agenzie viaggio, immobiliari, di brokeraggio, magazzini di custodia conto terzi)",
          "Alberghi, pubblici esercizi e attivita' similari", "Industria edile", "Artigianato edile", "Industria lapidei",
          "Artigianato lapidei", "Altro"]
FILE = {
    "cig_territori": {"nome": "Ore autorizzate di cassa integrazione per provincia lombarda, Lombardia e Italia (cumulate da inizio anno, INPS)",
                      "colonne": ["anno", "periodo", "tipo", "territorio", "ore"]},
    "cig_classi": {"nome": "Ore autorizzate di cassa integrazione di Varese per classe di attività economica (cumulate da inizio anno, INPS)",
                   "colonne": ["anno", "periodo", "tipo", "classe", "ore"]},
    "cig_varese_mensile": {"nome": "Ore autorizzate di cassa integrazione di Varese per mese (INPS)",
                           "colonne": ["anno", "mese", "tipo", "ore"]},
}
TIMEOUT = 180            # l'INPS a volte è molto lento (una richiesta è rimasta ferma 17 minuti)
PAUSE = [30, 90, 180]    # pause fra i tentativi (secondi)
PAUSA_FRA_RICHIESTE = 1
ORE_CADENZA_MASSIMA = 31 * 24     # se l'ultimo scarico completo è più vecchio, si riscarica anche se l'INPS dice «nessuna novità»


class Riservato(Exception):
    """L'INPS non mostra la tavola per le regole sulla riservatezza (art. 4 delle regole deontologiche SISTAN)."""


class Client:
    def __init__(self, pause=None):
        self.pause = list(PAUSE if pause is None else pause)
        self.richieste = 0

    def chiama(self, endpoint, corpo):
        dati = json.dumps(corpo, separators=(",", ":")).encode()      # il server vuole il JSON compatto (con spazi risponde «Base-64»)
        errore = None
        for n in range(len(self.pause) + 1):
            try:
                if self.richieste:
                    time.sleep(PAUSA_FRA_RICHIESTE)
                self.richieste += 1
                req = urllib.request.Request(API + endpoint + "/", data=dati, headers={
                    "Content-Type": "application/json", "Accept": "application/json, text/plain, */*", "Accept-Encoding": "gzip",
                    "User-Agent": "Mozilla/5.0 (report-lavoro; Camera di Commercio di Varese)"})
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    raw = r.read()
                    if r.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                break
            except Exception as e:  # rete, 5xx, timeout
                errore = e
                print(f"  tentativo {n + 1} non riuscito: {e}", flush=True)
                if n < len(self.pause):
                    time.sleep(self.pause[n])
        else:
            self.pause = []          # il sito è probabilmente fermo: per le altre richieste un solo tentativo
            n = len(PAUSE) + 1
            raise Problema("rete", f"l'INPS non ha risposto ({n} tentativi): {errore}")
        testo = raw.decode("utf-8", "replace")
        try:
            return json.loads(testo)
        except ValueError:
            raise Problema("risposta", f"l'INPS ha risposto, ma non con i dati (inizio della risposta: «{' '.join(testo[:150].split())}»)")

    def struttura(self, oss):
        d = self.chiama("getStrutturaOsservatorio", {"id_osservatorio": str(oss), "language": "it"})
        if "definition" not in d:
            raise Problema("risposta", f"l'INPS non ha mandato la struttura dell'osservatorio {oss}: {json.dumps(d, ensure_ascii=False)[:150]}")
        return d

    def tabella(self, oss, righe, colonne, filtri):
        """Una tavola dell'Osservatorio -> {(valore riga, valore colonna o None): ore}. filtri: [(campo, gerarchia, [valori])]."""
        def dim(nomi):
            return [{"id": n, "label": n, "order": i + 1, "aggregate": True, "expand": "", "hide": False} for i, n in enumerate(nomi)]
        corpo = {"id_osservatorio": str(oss), "nome_osservatorio": NOME_OSS, "language": "", "totalRow": True, "totalColumn": True,
                 "subtotalRow": True, "subtotalColumn": True,
                 "selections": {"rows": dim(righe), "cols": dim(colonne),
                                "measures": [{"id": "oretotSUM", "label": "oretotSUM", "order": 1, "statistic": "SUM"}],
                                "filters": [{"id": i, "label": l, "values": v} for i, l, v in filtri]}}
        d = self.chiama("getDatiOsservatorio", corpo)
        if d.get("messageType") == "Anonimizzazione":
            raise Riservato(d.get("message", ""))
        if "values" not in d:
            raise Problema("formato", f"l'INPS ha risposto con un errore ({d.get('errorCode') or d.get('error') or d.get('message')}): "
                                      f"la richiesta non è più accettata, forse il servizio è cambiato")
        out = {}
        for r in d["values"]:
            if "columns" in r:
                for c in r["columns"][0]["values"]:
                    out[(r["value"], c["value"])] = ore(c["measures"][0]["value"])
            else:
                out[(r["value"], None)] = ore(r["measures"][0]["value"])
        return out


def ore(testo):
    t = str(testo).strip()
    if t in ("-", ""):
        return 0
    try:
        return int(t.replace(".", ""))
    except ValueError:
        raise Problema("formato", f"valore non numerico nei dati INPS («{t}»)")


def data_agg(struttura):
    d = str(struttura.get("DateAgg") or "")
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else None


def controlla_struttura(s, vecchio=False):
    """Campi, valori e misura che la richiesta usa. Se l'INPS li cambia, i dati non vanno più letti alla cieca."""
    campi = {f["id"]: f for f in s["definition"]["fields"]}
    mancano = [c for c in ("gestio", "csc_new", "regione", "sigprov", "anno", "mese", "oretotSUM") if c not in campi]
    if mancano:
        raise Problema("formato", f"nella struttura dell'osservatorio {s.get('id_osservatorio')} mancano i campi {', '.join(mancano)} (l'INPS ha cambiato la tavola)")
    if set(TIPI) - set(campi["gestio"]["distinctValues"]):
        raise Problema("formato", f"i tipi di intervento dell'INPS sono cambiati: {campi['gestio']['distinctValues']}")
    classi = [c for c in campi["csc_new"]["distinctValues"] if not c.isdigit()]
    if classi != CLASSI:
        diff = sorted(set(classi) ^ set(CLASSI))
        raise Problema("formato", f"le classi di attività economica dell'INPS sono cambiate (differenze: {'; '.join(diff)[:300]})")
    attese = {"Varese"} if vecchio else set(PROVINCE)       # la serie 2009–2022 serve solo per Varese (ha 103 province: Monza non c'è)
    if attese - set(campi["sigprov"]["distinctValues"]):
        raise Problema("formato", f"nell'elenco delle province dell'INPS mancano: {', '.join(sorted(attese - set(campi['sigprov']['distinctValues'])))}")
    if "Lombardia" not in campi["regione"]["distinctValues"] or "Gennaio" not in campi["mese"]["distinctValues"]:
        raise Problema("formato", "regioni o mesi diversi da quelli attesi")
    return sorted(int(a) for a in campi["anno"]["distinctValues"])


def periodo_pubblicato(ultimo, anno, q):
    """Il trimestre q dell'anno è completo se l'ultimo mese pubblicato dall'INPS è almeno quello di fine trimestre."""
    return anno * 12 + q * 3 <= ultimo


def scarica_tutto(c, s_nuovo, s_vecchio, anni, vecchio_gia_salvato):
    """Tutte le richieste -> (righe dei tre file, ultimo mese pubblicato come anno*12+mese, riservati)."""
    filtri_anno = ("anno", "Anno-", [str(a) for a in anni])
    riservati = []
    # ultimo mese pubblicato: l'ultimo con ore autorizzate in Italia (i mesi non ancora pubblicati sono a zero)
    ita_mese = c.tabella(OSS_NUOVO, ["Anno-"], ["Mese-"], [filtri_anno])
    attivi = [int(a) * 12 + MESI.index(m) + 1 for (a, m), v in ita_mese.items() if a.isdigit() and m in MESI and v > 0]
    if not attivi:
        raise Problema("formato", "nei dati dell'INPS non risultano ore autorizzate in Italia (tavola vuota)")
    ultimo = max(attivi)
    print(f"ultimo mese pubblicato dall'INPS: {MESI[(ultimo - 1) % 12]} {(ultimo - 1) // 12}", flush=True)
    territori, classi, mensile = [], [], []
    for tipo in TIPI:
        for q in (1, 2, 3, 4):
            base = [("gestio", "Tipo intervento", [tipo]), filtri_anno, ("mese", "Mese-", MESI[:3 * q])]
            for nome, righe, geo in (("territori", ["Provincia"], [("regione", "Regione", ["Lombardia"])]),
                                     ("italia", ["Gestio"], []), ("classi", ["Csc_new"], [("sigprov", "Provincia", ["Varese"])])):
                if all(not periodo_pubblicato(ultimo, a, q) for a in anni):
                    continue
                try:
                    t = c.tabella(OSS_NUOVO, righe, ["Anno-"], geo + base)
                except Riservato:
                    riservati.append(f"{nome} {tipo} trimestre {q}")
                    print(f"  riservato dall'INPS: {nome} {tipo} trimestre {q}", flush=True)
                    continue
                for (riga, col), v in t.items():
                    if not (col or "").isdigit() or not periodo_pubblicato(ultimo, int(col), q):
                        continue
                    a = int(col)
                    if nome == "territori":
                        if riga in PROVINCE:
                            territori.append([a, q, tipo, riga, v])
                    elif nome == "italia":
                        if riga == tipo:
                            territori.append([a, q, tipo, "Italia", v])
                    elif riga in CLASSI:
                        classi.append([a, q, tipo, riga, v])
    # Lombardia = somma delle 12 province (l'INPS rifiuta alcune tavole regionali con pochi dati)
    somma = {}
    n_prov = {}
    for a, q, tipo, t, v in territori:
        if t in PROVINCE:
            somma[(a, q, tipo)] = somma.get((a, q, tipo), 0) + v
            n_prov[(a, q, tipo)] = n_prov.get((a, q, tipo), 0) + 1
    for (a, q, tipo), v in somma.items():
        if n_prov[(a, q, tipo)] != len(PROVINCE):
            raise Problema("formato", f"per {tipo} {a} trimestre {q} l'INPS ha {n_prov[(a, q, tipo)]} province lombarde invece di {len(PROVINCE)}")
        territori.append([a, q, tipo, "Lombardia", v])
    # Varese mese per mese: 2023→oggi dalla serie nuova, 2009–2022 dalla serie vecchia (ferma al 17/12/2020: si riscarica solo se manca)
    for tipo in TIPI:
        filtri = [("sigprov", "Provincia", ["Varese"]), ("gestio", "Tipo intervento", [tipo])]
        t = c.tabella(OSS_NUOVO, ["Anno-"], ["Mese-"], filtri)
        for (a, m), v in t.items():
            if a.isdigit() and m in MESI and int(a) * 12 + MESI.index(m) + 1 <= ultimo:
                mensile.append([int(a), MESI.index(m) + 1, tipo, v])
        if vecchio_gia_salvato is None:
            t = c.tabella(OSS_VECCHIO, ["Anno-"], ["Mese-"], filtri)
            for (a, m), v in t.items():
                if a.isdigit() and m in MESI:
                    mensile.append([int(a), MESI.index(m) + 1, tipo, v])
    if vecchio_gia_salvato is not None:
        mensile += vecchio_gia_salvato
    return territori, classi, mensile, ultimo, riservati


def controlla_coerenza(territori, classi, mensile, anni):
    """Varese deve dare lo stesso numero da tre tavole diverse (province, classi, mesi): se no, qualcosa è cambiato."""
    var = {(a, q, t): v for a, q, t, p, v in territori if p == "Varese"}
    cls = {}
    for a, q, t, k, v in classi:
        cls[(a, q, t)] = cls.get((a, q, t), 0) + v
    mes = {}
    for a, m, t, v in mensile:
        for q in (1, 2, 3, 4):
            if m <= 3 * q:
                mes[(a, q, t)] = mes.get((a, q, t), 0) + v
    for k, v in var.items():
        if k in cls and cls[k] != v:
            raise Problema("anomalia", f"Varese {k[2]} {k[0]} trimestre {k[1]}: la somma delle classi ({cls[k]}) non è uguale al dato provinciale ({v})")
        # i mesi mancanti dell'anno non sono un problema: si confronta solo se ci sono tutti i mesi del trimestre
        if len({m for a, m, t, _ in mensile if a == k[0] and t == k[2] and m <= 3 * k[1]}) == 3 * k[1] and mes[k] != v:
            raise Problema("anomalia", f"Varese {k[2]} {k[0]} trimestre {k[1]}: la somma dei mesi ({mes[k]}) non è uguale al dato cumulato ({v})")
    ita = {(a, q, t): v for a, q, t, p, v in territori if p == "Italia"}
    lom = {(a, q, t): v for a, q, t, p, v in territori if p == "Lombardia"}
    for k, v in lom.items():
        if k in ita and v > ita[k]:
            raise Problema("anomalia", f"{k[2]} {k[0]} trimestre {k[1]}: la Lombardia ({v}) supera l'Italia ({ita[k]})")


def leggi_csv(percorso, nomi_numeri):
    if not os.path.exists(percorso):
        return None
    with open(percorso, encoding="utf-8", newline="") as f:
        return [[int(r[n]) if n in nomi_numeri else r[n] for n in r] for r in csv.DictReader(f)]


def main():
    arg = argparse.ArgumentParser(description="Scarica dall'INPS le ore autorizzate di cassa integrazione")
    arg.add_argument("--esito", help="file JSON in cui scrivere l'esito (lo legge segnala_problemi.py)")
    arg.add_argument("--forza", action="store_true", help="scarica anche se l'INPS non segnala novità")
    a = arg.parse_args()
    prova = os.environ.get("PROVA_ERRORE", "").lower() == "true"
    os.makedirs(CARTELLA, exist_ok=True)
    p_info = os.path.join(CARTELLA, "aggiornamento.json")
    info = {}
    if os.path.exists(p_info):
        with open(p_info, encoding="utf-8") as f:
            info = json.load(f)
    adesso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    problemi, tavole = [], []
    salvato = info.get("cig_territori", {})
    prima = salvato.get("ultimo_mese")
    cod = "cig"
    nome = "Ore autorizzate di cassa integrazione (Osservatorio INPS)"
    try:
        if prova:
            raise Problema("prova", "errore simulato per provare l'e-mail di avviso (nessun problema reale)")
        c = Client()
        s_nuovo = c.struttura(OSS_NUOVO)
        anni = controlla_struttura(s_nuovo)
        agg = data_agg(s_nuovo)
        agg_vecchio = None
        percorsi = {k: os.path.join(CARTELLA, k + ".csv") for k in FILE}
        presenti = all(os.path.exists(p) for p in percorsi.values())
        recente = False
        if salvato.get("scaricato"):
            eta = (datetime.strptime(adesso, "%Y-%m-%dT%H:%M:%SZ") - datetime.strptime(salvato["scaricato"], "%Y-%m-%dT%H:%M:%SZ")).total_seconds() / 3600
            recente = eta < ORE_CADENZA_MASSIMA
        print(f"INPS: dati aggiornati al {agg}; salvati: {salvato.get('ultimo_aggiornamento_inps')}", flush=True)
        if presenti and recente and not a.forza and salvato.get("ultimo_aggiornamento_inps") == agg:
            print("Nessuna novità dell'INPS: niente da scaricare.", flush=True)
            tavole.append({"tavola": cod, "nome": nome, "ultimo_anno": salvato.get("ultimo_mese"), "prima": prima, "cambiato": False})
        else:
            # la serie 2009–2022 non cambia più: la si riscarica solo se manca
            s_vecchio = c.struttura(OSS_VECCHIO)
            controlla_struttura(s_vecchio, vecchio=True)
            agg_vecchio = data_agg(s_vecchio)
            mensile_salvato = leggi_csv(percorsi["cig_varese_mensile"], {"anno", "mese", "ore"})
            vecchio = None
            if mensile_salvato and salvato.get("ultimo_aggiornamento_inps_serie_2009_2022") == agg_vecchio:
                vecchio = [r for r in mensile_salvato if r[0] <= 2022]
            territori, classi, mensile, ultimo, riservati = scarica_tutto(c, s_nuovo, s_vecchio, anni, vecchio)
            controlla_coerenza(territori, classi, mensile, anni)
            if prima and ultimo < int(prima[:4]) * 12 + int(prima[5:7]):
                raise Problema("anomalia", f"i dati scaricati arrivano a {MESI[(ultimo - 1) % 12]} {(ultimo - 1) // 12}, quelli già salvati a {prima}: tenuti quelli salvati")
            nuovi = {"cig_territori": sorted(territori, key=lambda r: (r[0], r[1], TIPI.index(r[2]), r[3])),
                     "cig_classi": sorted(classi, key=lambda r: (r[0], r[1], TIPI.index(r[2]), CLASSI.index(r[3]))),
                     "cig_varese_mensile": sorted(mensile, key=lambda r: (r[0], r[1], TIPI.index(r[2])))}
            for k, righe in nuovi.items():
                if salvato.get("righe") and k == "cig_territori" and len(righe) < 0.9 * salvato["righe"]:
                    raise Problema("anomalia", f"i dati scaricati hanno {len(righe)} valori (territori), quelli già salvati {salvato['righe']}: tenuti quelli salvati")
            ultimo_txt = f"{(ultimo - 1) // 12}-{(ultimo - 1) % 12 + 1:02d}"
            cambiato = False
            for k, righe in nuovi.items():
                testo = testo_csv(righe, FILE[k]["colonne"])
                p = percorsi[k]
                vecchio_testo = open(p, encoding="utf-8").read() if os.path.exists(p) else None
                mod = vecchio_testo != testo
                cambiato = cambiato or mod
                if mod:
                    with open(p + ".tmp", "w", encoding="utf-8", newline="") as f:
                        f.write(testo)
                    os.replace(p + ".tmp", p)
                vecchi = info.get(k, {})
                info[k] = {"nome": FILE[k]["nome"], "scaricato": adesso, "ultimo_mese": ultimo_txt, "righe": len(righe),
                           "ultimo_aggiornamento_inps": agg, "ultimo_aggiornamento_inps_serie_2009_2022": agg_vecchio,
                           "dati_modificati": adesso if mod else vecchi.get("dati_modificati", adesso)}
                if riservati:
                    info[k]["tavole_riservate"] = riservati
            tavole.append({"tavola": cod, "nome": nome, "ultimo_anno": ultimo_txt, "prima": prima, "cambiato": cambiato})
            print(f"INPS: fino a {ultimo_txt}; territori {len(nuovi['cig_territori'])}, classi {len(nuovi['cig_classi'])}, mesi {len(nuovi['cig_varese_mensile'])} righe; "
                  f"{'MODIFICATO' if cambiato else 'invariato'}", flush=True)
            if riservati:
                problemi.append({"tavola": cod, "nome": nome, "tipo": "riservato", "ultimo_anno_salvato": prima,
                                 "dettaglio": "tavole non mostrate dall'INPS per riservatezza (saltate, il resto è stato salvato): " + "; ".join(riservati)})
    except Problema as e:
        problemi.append({"tavola": cod, "nome": nome, "tipo": e.tipo, "dettaglio": str(e), "ultimo_anno_salvato": prima})
        print(f"{cod}: PROBLEMA ({e.tipo}) {e}", flush=True)
    except Exception as e:  # imprevisto: lo si segnala come tale
        problemi.append({"tavola": cod, "nome": nome, "tipo": "imprevisto", "dettaglio": f"{type(e).__name__}: {e}", "ultimo_anno_salvato": prima})
        print(f"{cod}: ERRORE IMPREVISTO {type(e).__name__}: {e}", flush=True)
    info["controllato_cig"] = adesso
    with open(p_info, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
        f.write("\n")
    if a.esito:
        with open(a.esito, "w", encoding="utf-8") as f:
            json.dump({"quando": adesso, "prova": prova, "problemi": problemi, "tavole": tavole}, f, ensure_ascii=False, indent=1)
    if problemi:
        print("\n".join(f"{p['tavola']}: {p['dettaglio']}" for p in problemi), file=sys.stderr)
        if not a.esito:
            sys.exit(1)


if __name__ == "__main__":
    main()
