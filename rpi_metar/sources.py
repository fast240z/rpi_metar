import csv
import logging
import re
import requests
import time
import os
import ssl
import threading
import json
from pkg_resources import resource_filename
from retrying import retry
from xmltodict import parse as parsexml
import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)


def chunks(l, n):
    """Yield successive n-sized chunks from l."""
    for i in range(0, len(l), n):
        yield l[i:i + n]


class METARSource:

    @retry(wait_exponential_multiplier=1000,
           wait_exponential_max=10000,
           stop_max_attempt_number=10)
    def _query(self):
        """Queries a URL."""
        try:
            headers = getattr(self, "headers", {})
            response = requests.get(self.url, headers=headers, timeout=10.0)
            response.raise_for_status()
        except:
            log.exception('Query failure.')
            raise
        return response


class NOAA(METARSource):

    URL = (
        "https://aviationweather.gov/api/data/metar"
        "?ids={airport_codes}"
        "&format=xml"
    )

    def __init__(self, airport_codes, **kwargs):
        self.airport_codes = airport_codes
        self.headers = {"User-Agent": "metarmap/0.4.1"}

    def get_metar_info(self):
        metars = {}
        for chunk in chunks(self.airport_codes, 250):
            self.url = self.URL.format(airport_codes=",".join(chunk))
            response = self._query()
            try:
                response = parsexml(response.text)['response']['data']['METAR']
                if not isinstance(response, list):
                    response = [response]
            except Exception:
                log.exception("Metar response is invalid.")
                raise
            finally:
                time.sleep(1.0)
            for m in response:
                metars[m['station_id'].upper()] = m
        log.info(f"Retrieved NOAA METARs: {len(metars)} stations")
        return metars


class NOAABackup(NOAA):
    def __init__(self, airport_codes, **kwargs):
        super(NOAABackup, self).__init__(airport_codes, subdomain='bcaws', **kwargs)


class SkyVector(METARSource):

    URL = (
        'https://skyvector.com/api/dLayer'
        '?ll1={lat1},{lon1}'
        '&ll2={lat2},{lon2}'
        '&layers=metar'
    )

    def _find_coordinates(self):
        data = {}
        file_name = resource_filename('rpi_metar', 'data/us-airports.csv')
        with open(file_name, newline='') as csvfile:
            reader = csv.reader(csvfile)
            for row in reader:
                airport_code, lat, lon = row
                if airport_code in self.airport_codes:
                    data[airport_code] = (lat, lon)
        self.data = data
        lat1 = min((float(lat) for lat, _ in data.values()))
        lon1 = min((float(lon) for _, lon in data.values()))
        lat2 = max((float(lat) for lat, _ in data.values()))
        lon2 = max((float(lon) for _, lon in data.values()))
        lat1, lon1 = map(lambda x: x - 0.5, [lat1, lon1])
        lat2, lon2 = map(lambda x: x + 0.5, [lat2, lon2])
        self.url = SkyVector.URL.format(lat1=lat1, lon1=lon1, lat2=lat2, lon2=lon2)

    def __init__(self, airport_codes, **kwargs):
        self.airport_codes = [code.upper() for code in airport_codes]
        self._find_coordinates()

    def get_metar_info(self):
        response = self._query()
        try:
            data = response.json()['weather']
        except:
            log.exception('Metar response is invalid.')
            raise
        metars = {}
        for item in data:
            if item['s'] in self.airport_codes:
                metars[item['s'].upper()] = {'raw_text': item['m']}
        return metars


class BOM(METARSource):
    URL = 'http://www.bom.gov.au/aviation/php/process.php'

    def __init__(self, airport_codes, **kwargs):
        self.airport_codes = ','.join(airport_codes)

    def get_metar_info(self):
        payload = {'keyword': self.airport_codes, 'type': 'search', 'page': 'TAF'}
        r = requests.post(self.URL, data=payload)
        matches = re.finditer(r'(?:METAR |SPECI )(?P<METAR>(?P<CODE>\w{4}).*?)(?:<br />|<h3>)', r.text)
        metars = {}
        for match in matches:
            info = match.groupdict()
            metars[info['CODE'].upper()] = {'raw_text': info['METAR']}
        return metars


class IFIS(METARSource):
    URL = 'https://www.ifis.airways.co.nz/script/briefing/met_briefing_proc.asp'
    LOGIN_URL = 'https://www.ifis.airways.co.nz/secure/script/user_reg/login_proc.asp'
    ACCEPTED_CODES = {
        'NZCH', 'NZCI', 'NZAA', 'NZDN', 'NZGS', 'NZHN', 'NZHK', 'NZNV', 'NZKK',
        'NZMS', 'NZMF', 'NZNR', 'NZNS', 'NZNP', 'NZOU', 'NZOH', 'NZPM', 'NZPP',
        'NZQN', 'NZRO', 'NZAP', 'NZTG', 'NZMO', 'NZTU', 'NZWF', 'NZWN', 'NZWS',
        'NZWK', 'NZWU', 'NZWR', 'NZWP', 'NZWB'
    }

    def __init__(self, airport_codes, *, config, **kwargs):
        self.airport_codes = ' '.join([code for code in airport_codes if code in IFIS.ACCEPTED_CODES])
        self.username = config['ifis']['username']
        self.password = config['ifis']['password']
        self.login_payload = {'UserName': self.username, 'Password': self.password}
        self.data_payload = {'METAR': 1, 'MetLocations': self.airport_codes}

    def get_metar_info(self):
        with requests.Session() as session:
            session.post(self.LOGIN_URL, data=self.login_payload)
            r = session.post(self.URL, data=self.data_payload)
            log.info(r.text)
        matches = re.finditer(r'(?:METAR |SPECI )(?P<METAR>(?P<CODE>\w{4}).*?)(?:<br/>|<h3>|=</span>|<br />)', r.text)
        metars = {}
        for match in matches:
            info = match.groupdict()
            metars[info['CODE'].upper()] = {'raw_text': info['METAR']}
        return metars


# ---------------- Mesotech Class Using MQTT ----------------

class Mesotech(METARSource):

    ACCEPTED_CODES = {"KO61"}  # supported codes

    def __init__(self, airport_codes, **kwargs):
        self.airport_codes = [code for code in airport_codes if code in self.ACCEPTED_CODES]
        self.latest_omo = {}
        self.lock = threading.Lock()

        # Read secrets from environment
        awos_user = os.getenv("AWOS_USER")
        awos_pass = os.getenv("AWOS_PASS")

        # MQTT client setup
        self.client = mqtt.Client(client_id="mesotech_fetcher", transport="websockets")
        self.client.username_pw_set(awos_user, awos_pass)
        self.client.ws_set_options(path="/", headers={"Origin": "https://ko61.awos.live"})
        self.client.tls_set(cert_reqs=ssl.CERT_NONE)
        self.client.tls_insecure_set(True)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

        self.client.connect("mqtt.awos.live", 8083, 60)
        self.client.loop_start()

    def on_connect(self, client, userdata, flags, rc, properties=None):
        topic = f"AWA/KO61/ReportData"
        client.subscribe(topic)

    def on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            omo_report = payload.get("omo_report")
            if omo_report:
                with self.lock:
                    self.latest_omo["KO61"] = {"raw_text": omo_report}
        except Exception:
            pass

    def get_metar_info(self):
        # Wait up to 5 seconds for latest message
        timeout = 5
        start = time.time()
        while time.time() - start < timeout:
            with self.lock:
                if "KO61" in self.latest_omo:
                    return self.latest_omo.copy()
            time.sleep(0.1)
        # fallback: return last received or empty
        with self.lock:
            return self.latest_omo.copy()
