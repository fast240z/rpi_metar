import csv
import logging
import re
import requests
import time
import json
import ssl
import paho.mqtt.client as mqtt

from pkg_resources import resource_filename
from retrying import retry
from xmltodict import parse as parsexml

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
        """Queries the NOAA METAR service."""
        log.info(self.url)
        try:
            headers = getattr(self, "headers", {})
            response = requests.get(self.url, headers=headers, timeout=10.0)
            response.raise_for_status()
        except Exception:
            log.exception('Metar query failure.')
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
        self.headers = {
            "User-Agent": "metarmap/0.4.1"
        }

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
        super(NOAABackup, self).__init__(airport_codes, **kwargs)


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
        except Exception:
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
        payload = {
            'keyword': self.airport_codes,
            'type': 'search',
            'page': 'TAF',
        }

        r = requests.post(self.URL, data=payload)

        matches = re.finditer(
            r'(?:METAR |SPECI )(?P<METAR>(?P<CODE>\w{4}).*?)(?:<br />|<h3>)',
            r.text
        )

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
        self.airport_codes = ' '.join(
            [code for code in airport_codes if code in IFIS.ACCEPTED_CODES]
        )
        self.username = config['ifis']['username']
        self.password = config['ifis']['password']
        self.login_payload = {
            'UserName': self.username,
            'Password': self.password,
        }
        self.data_payload = {
            'METAR': 1,
            'MetLocations': self.airport_codes,
        }

    def get_metar_info(self):
        with requests.Session() as session:
            session.post(self.LOGIN_URL, data=self.login_payload)
            r = session.post(self.URL, data=self.data_payload)

        matches = re.finditer(
            r'(?:METAR |SPECI )(?P<METAR>(?P<CODE>\w{4}).*?)(?:<br/>|<h3>|=</span>|<br />)',
            r.text
        )

        metars = {}
        for match in matches:
            info = match.groupdict()
            metars[info['CODE'].upper()] = {'raw_text': info['METAR']}

        return metars


class Mesotech(METARSource):

    ACCEPTED_CODES = {
        'KO61', 'K4B8'
    }

    AWOS_HOST = "mqtt.awos.live"
    AWOS_PORT = 8083
    AWOS_USER = "AWA_Web_wVVdDr"
    AWOS_PASS = "Po&X58vexCkq;Wyp"

    def __init__(self, airport_codes, **kwargs):
        self.airport_codes = [
            code for code in airport_codes if code in self.ACCEPTED_CODES
        ]

    def _fetch_omo_report(self, icao, timeout=5):
        topic = f"AWA/{icao}/ReportData"
        result = {"omo": None}

        def on_connect(client, userdata, flags, rc, properties=None):
            if rc == 0:
                client.subscribe(topic)

        def on_message(client, userdata, msg):
            try:
                payload = json.loads(msg.payload.decode())
                omo = payload.get("omo_report")
                if omo:
                    result["omo"] = omo
                    client.disconnect()
            except Exception:
                pass

        client = mqtt.Client(transport="websockets")
        client.username_pw_set(self.AWOS_USER, self.AWOS_PASS)

        client.ws_set_options(
            path="/",
            headers={"Origin": f"https://{icao.lower()}.awos.live"}
        )

        client.tls_set(cert_reqs=ssl.CERT_NONE)
        client.tls_insecure_set(True)

        client.on_connect = on_connect
        client.on_message = on_message

        try:
            client.connect(self.AWOS_HOST, self.AWOS_PORT, 60)
            client.loop_start()

            start = time.time()
            while time.time() - start < timeout:
                if result["omo"]:
                    break
                time.sleep(0.1)

        finally:
            client.loop_stop()
            client.disconnect()

        return result["omo"]

    def get_metar_info(self):
        metars = {}

        for code in self.airport_codes:
            try:
                omo = self._fetch_omo_report(code)

                if omo:
                    if omo.startswith("OMO "):
                        omo = omo[4:]

                    metars[code] = {'raw_text': omo}

            except Exception:
                log.exception(f"Failed to retrieve METAR from {code}")

        return metars
