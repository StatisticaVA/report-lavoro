#!/usr/bin/env python3
"""Scarica da ISTAT i dati annuali della disoccupazione e salva in dati/ solo quello che serve alla pagina «Lavoro».

Lo usa l'aggiornamento automatico su GitHub (.github/workflows/aggiorna-dati.yml, ogni lunedì), ma si può lanciare
anche a mano:  python3 aggiorna_dati.py        Solo libreria standard di Python (niente da installare).

Perché serve: il servizio ISTAT (SDMXWS di IstatData) non permette a una pagina web di leggere i dati da sola
(niente intestazioni CORS), quindi li scarica questo programma e la pagina li legge dal proprio repository.

Per ogni tavola di TAVOLE:
  1. scarica il CSV SDMX (Rilevazione sulle forze di lavoro, dati annuali dal 2018): Varese, Lombardia, Italia e le
     12 province lombarde; la richiesta è la stessa del programma Python «Report Disoccupazione»;
  2. controlla che sia quello atteso (colonne, tipo di dato, territori) e che non sia più vecchio di quello salvato;
  3. scrive dati/<tavola>.csv (solo le colonne utili) e aggiorna dati/aggiornamento.json (con la data di ultimo
     aggiornamento dichiarata da ISTAT).
Se per una tavola qualcosa non va, il suo file NON viene toccato e il problema viene scritto nel file di esito (--esito):
le altre tavole si aggiornano lo stesso. Il workflow legge l'esito e, solo se c'è un problema, apre una segnalazione
chiara nel repository (GitHub la manda per e-mail): vedi segnala_problemi.py. Se ISTAT non ha pubblicato dati nuovi
non è un problema: nessuna segnalazione.
Con la variabile d'ambiente PROVA_ERRORE=true si simula un problema (per provare l'e-mail).
"""
import argparse
import csv
import io
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

SDMXWS = "https://esploradati.istat.it/SDMXWS/rest/"
CARTELLA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dati")
ANNO_INIZIO = 2018
PROVINCE = ["ITC41", "ITC42", "ITC44", "ITC45", "ITC46", "ITC47", "ITC48", "ITC4A", "ITC4B", "ITC43", "ITC49", "IT108"]
AREE = ["IT", "ITC4"] + PROVINCE          # Italia, Lombardia, province lombarde (ISTAT: Italia = IT)
SESSI = "1+2+9"                            # maschi, femmine, totale
FASCE = "+".join(["Y15-24", "Y25-34", "Y15-34", "Y35-49", "Y20-64", "Y15-64", "Y15-74", "Y50-74"])
TAVOLE = [
    {"codice": "disoccupazione_tassi", "nome": "Tasso di disoccupazione (Rilevazione sulle forze di lavoro, dati annuali)",
     "dataflow": "IT1,151_914,1.0", "tipo": "UNEM_R",
     "chiave": f"A.{'+'.join(AREE)}.UNEM_R.{SESSI}.{FASCE}.99.TOTAL.TOTAL"},
    {"codice": "disoccupazione_disoccupati", "nome": "Disoccupati in migliaia (Rilevazione sulle forze di lavoro, dati annuali)",
     "dataflow": "IT1,151_929_DF_DCCV_DISOCCUPT1_7,1.0", "tipo": "UNEMP",
     "chiave": f"A.{'+'.join(AREE)}.UNEMP.{SESSI}.Y15-74.99.TOTAL.99.TOTAL"},
]
COLONNE = ["REF_AREA", "DATA_TYPE", "SEX", "AGE", "TIME_PERIOD", "OBS_VALUE", "OBS_STATUS"]
INDISPENSABILI = ["REF_AREA", "DATA_TYPE", "SEX", "AGE", "TIME_PERIOD", "OBS_VALUE"]
TERRITORI_ATTESI = {"IT", "ITC4", "ITC41"}
ACCEPT_CSV = "application/vnd.sdmx.data+csv;version=1.0.0, text/csv"


class Problema(Exception):
    """Problema di una tavola. tipo: "rete" (ISTAT non risponde), "risposta" (risponde ma non con il CSV dei dati),
    "formato" (la tavola è cambiata: colonne, tipo di dato o territori diversi), "anomalia" (dati più vecchi o molto
    meno numerosi di quelli salvati), "prova" (simulato con PROVA_ERRORE)."""
    def __init__(self, tipo, dettaglio):
        super().__init__(dettaglio)
        self.tipo = tipo


# pause fra i tentativi (secondi): un disservizio breve di ISTAT non deve far partire l'e-mail.
# Se una tavola non si scarica nemmeno così, per le successive si fa un tentativo solo (il sito è probabilmente fermo).
PAUSE = [60, 180, 300]
TIMEOUT = 120
PAUSA_ISTAT = 13        # ISTAT accetta circa 5 richieste al minuto per indirizzo


def scarica(url, accept, pause):
    errore = None
    for n in range(len(pause) + 1):
        try:
            req = urllib.request.Request(url, headers={"Accept": accept, "Accept-Language": "it, en;q=0.5",
                                                       "User-Agent": "Mozilla/5.0 (report-lavoro; Camera di Commercio di Varese)"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read()
        except Exception as e:  # rete, 429, 5xx, timeout
            errore = e
            print(f"  tentativo {n + 1} non riuscito: {e}", flush=True)
            if n < len(pause):
                time.sleep(pause[n])
    n = len(pause) + 1
    raise Problema("rete", f"ISTAT non ha risposto ({n} {'tentativo' if n == 1 else 'tentativi'}): {errore}")


def leggi_dati(dati, tavola):
    """CSV di ISTAT -> (righe tenute come liste di COLONNE, ultimo anno)."""
    testo = dati.decode("utf-8-sig", "replace")
    inizio = testo[:200].strip().replace("\n", " ")
    prima = testo.split("\n", 1)[0]
    if not any(c in prima for c in INDISPENSABILI):       # nessuna colonna riconoscibile: non è il CSV dei dati (es. pagina di manutenzione)
        raise Problema("risposta", f"ISTAT ha risposto, ma non con il file CSV dei dati (inizio della risposta: «{inizio[:120]}»)")
    lettore = csv.DictReader(io.StringIO(testo))
    mancano = [c for c in INDISPENSABILI if c not in (lettore.fieldnames or [])]
    if mancano:
        raise Problema("formato", f"nel CSV mancano le colonne {', '.join(mancano)} (ISTAT ha cambiato la tavola)")
    righe = []
    for r in lettore:
        if r["OBS_VALUE"] in ("", None):
            continue
        if r["DATA_TYPE"] != tavola["tipo"]:
            raise Problema("formato", f"nel CSV c'è il tipo di dato «{r['DATA_TYPE']}» invece di «{tavola['tipo']}» (ISTAT ha cambiato la tavola)")
        try:
            float(r["OBS_VALUE"])
            anno = int(r["TIME_PERIOD"])
        except ValueError:
            raise Problema("formato", f"valore o periodo non numerico nel CSV (periodo «{r['TIME_PERIOD']}», valore «{r['OBS_VALUE']}»)")
        righe.append([r.get(c, "") or "" for c in COLONNE])
    if not righe:
        raise Problema("formato", "nel CSV non ci sono valori")
    territori = {r[0] for r in righe}
    if not TERRITORI_ATTESI <= territori:
        raise Problema("formato", f"nel CSV mancano i territori {', '.join(sorted(TERRITORI_ATTESI - territori))} (Italia, Lombardia o Varese)")
    righe.sort(key=lambda r: (r[0], r[2], r[3], r[4]))
    return righe, max(int(r[4]) for r in righe)


def ultimo_aggiornamento(dataflow):
    """Data di ultimo aggiornamento dichiarata da ISTAT per il dataflow (facoltativa: se non c'è, None)."""
    try:
        xml = scarica(f"{SDMXWS}dataflow/{dataflow.replace(',', '/')}?references=none", "application/xml", []).decode("utf-8", "replace")
        i = xml.find('id="LAST_UPDATE"')
        a = xml.find("<common:AnnotationTitle>", i)
        b = xml.find("</common:AnnotationTitle>", a)
        return xml[a + 24:b] if i > 0 and a > 0 and b > a else None
    except Exception:
        return None


def testo_csv(righe):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(COLONNE)
    w.writerows(righe)
    return buf.getvalue()


def main():
    arg = argparse.ArgumentParser(description="Scarica da ISTAT i dati della disoccupazione")
    arg.add_argument("--esito", help="file JSON in cui scrivere l'esito (lo legge segnala_problemi.py)")
    esito_path = arg.parse_args().esito
    prova = os.environ.get("PROVA_ERRORE", "").lower() == "true"
    os.makedirs(CARTELLA, exist_ok=True)
    p_info = os.path.join(CARTELLA, "aggiornamento.json")
    info = {}
    if os.path.exists(p_info):
        with open(p_info, encoding="utf-8") as f:
            info = json.load(f)
    adesso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    anno_fine = datetime.now(timezone.utc).year
    problemi, tavole = [], []
    pause = PAUSE
    for k, t in enumerate(TAVOLE):
        cod = t["codice"]
        salvato = info.get(cod, {})
        prima = salvato.get("ultimo_anno")
        print(f"{cod}: scarico…", flush=True)
        try:
            if prova and cod == TAVOLE[-1]["codice"]:
                raise Problema("prova", "errore simulato per provare l'e-mail di avviso (nessun problema reale)")
            if k:
                time.sleep(PAUSA_ISTAT)
            try:
                dati = scarica(f"{SDMXWS}data/{t['dataflow']}/{t['chiave']}?startPeriod={ANNO_INIZIO}&endPeriod={anno_fine}",
                               ACCEPT_CSV, pause)
            except Problema:
                pause = []
                raise
            righe, ultimo = leggi_dati(dati, t)
            if prima and ultimo < prima:
                raise Problema("anomalia", f"i dati scaricati arrivano al {ultimo}, quelli già salvati al {prima}: tenuti quelli salvati")
            if salvato.get("righe") and len(righe) < 0.9 * salvato["righe"]:
                raise Problema("anomalia", f"i dati scaricati hanno {len(righe)} valori, quelli già salvati {salvato['righe']}: tenuti quelli salvati")
            time.sleep(PAUSA_ISTAT)
            agg = ultimo_aggiornamento(t["dataflow"])
            percorso = os.path.join(CARTELLA, f"{cod}.csv")
            nuovo = testo_csv(righe)
            vecchio = open(percorso, encoding="utf-8").read() if os.path.exists(percorso) else None
            cambiato = vecchio != nuovo
            if cambiato:
                with open(percorso + ".tmp", "w", encoding="utf-8", newline="") as f:
                    f.write(nuovo)
                os.replace(percorso + ".tmp", percorso)
            info[cod] = {"nome": t["nome"], "scaricato": adesso, "ultimo_anno": ultimo, "righe": len(righe),
                         "ultimo_aggiornamento_istat": agg,
                         "dati_modificati": adesso if cambiato else salvato.get("dati_modificati", adesso)}
            tavole.append({"tavola": cod, "nome": t["nome"], "ultimo_anno": ultimo, "prima": prima, "cambiato": cambiato})
            print(f"{cod}: {len(righe)} valori, ultimo anno {ultimo}, ultimo aggiornamento ISTAT {agg}, {'MODIFICATO' if cambiato else 'invariato'}", flush=True)
        except Problema as e:
            problemi.append({"tavola": cod, "nome": t["nome"], "tipo": e.tipo, "dettaglio": str(e), "ultimo_anno_salvato": prima})
            print(f"{cod}: PROBLEMA ({e.tipo}) {e}", flush=True)
        except Exception as e:  # imprevisto: lo si segnala come tale
            problemi.append({"tavola": cod, "nome": t["nome"], "tipo": "imprevisto", "dettaglio": f"{type(e).__name__}: {e}", "ultimo_anno_salvato": prima})
            print(f"{cod}: ERRORE IMPREVISTO {type(e).__name__}: {e}", flush=True)
    info["controllato"] = adesso
    with open(p_info, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
        f.write("\n")
    if esito_path:
        with open(esito_path, "w", encoding="utf-8") as f:
            json.dump({"quando": adesso, "prova": prova, "problemi": problemi, "tavole": tavole}, f, ensure_ascii=False, indent=1)
    if problemi:
        print("\n".join(f"{p['tavola']}: {p['dettaglio']}" for p in problemi), file=sys.stderr)
        # senza file di esito (uso a mano) si termina con errore; con l'esito ci pensa segnala_problemi.py
        if not esito_path:
            sys.exit(1)


if __name__ == "__main__":
    main()
