import csv
import logging
import re
import requests
import time
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from pkg_resources import resource_filename
from retrying import retry
from xmltodict import parse as parsexml

# Set up Chrome options
options = webdriver.ChromeOptions()
options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
options.add_argument('--disable-gpu')
options.add_argument('--remote-debugging-port=9222')
options.binary_location = "/usr/bin/chromium-browser"
service = Service("/usr/bin/chromedriver")
driver = webdriver.Chrome(service=service, options=options)

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
            response = requests.get(self.url, timeout=10.0)
            response.raise_for_status()
        except:
            log.exception('Metar query failure.')
            raise
        return response


class NOAA(METARSource):

    URL = (
        'https://{subdomain}.aviationweather.gov/cgi-bin/data/dataserver.php'
        '?dataSource=metars'
        '&requestType=retrieve'
        '&format=xml'
        '&hoursBeforeNow=2'
        '&mostRecentForEachStation=true'
        '&stationString={airport_codes}'
    )

    def __init__(self, airport_codes, subdomain='www', **kwargs):
        self.airport_codes = airport_codes
        self.subdomain = subdomain

    def get_metar_info(self):
        metars = {}

        for chunk in chunks(self.airport_codes, 250):
            self.url = self.URL.format(airport_codes=','.join(chunk), subdomain=self.subdomain)
            response = self._query()
            try:
                response = parsexml(response.text)['response']['data']['METAR']
                if not isinstance(response, list):
                    response = [response]
            except:
                log.exception('Metar response is invalid.')
                raise
            finally:
                time.sleep(1.0)

            for m in response:
                metars[m['station_id'].upper()] = m

        log.info(f"Retrieved NOAA METARs: {metars}")
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
        payload = {
            'keyword': self.airport_codes,
            'type': 'search',
            'page': 'TAF',
        }

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
            log.info(r.text)

        matches = re.finditer(r'(?:METAR |SPECI )(?P<METAR>(?P<CODE>\w{4}).*?)(?:<br/>|<h3>|=</span>|<br />)', r.text)

        metars = {}
        for match in matches:
            info = match.groupdict()
            metars[info['CODE'].upper()] = {'raw_text': info['METAR']}

        return metars


class Mesotech(METARSource):

    ACCEPTED_CODES = {
        'KO61', 'K4B8'  # Add other supported codes here as needed
    }

    def __init__(self, airport_codes, **kwargs):
        self.airport_codes = [code for code in airport_codes if code in self.ACCEPTED_CODES]

    def get_metar_info(self):
        metars = {}

        try:
            for code in self.airport_codes:
                url = f"https://{code.lower()}.awos.live"
                driver.get(url)

                element = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "td#OfficialObs.Value"))
                )

                full_text = element.text
                match = re.search(r'OMO\s+(.*)', full_text)

                if match:
                    metars[code] = {'raw_text': match.group(1)}

        except Exception:
            log.exception("Failed to retrieve METAR from KO61.")

        return metars

