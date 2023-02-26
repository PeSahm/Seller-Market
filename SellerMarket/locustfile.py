from locust import FastHttpUser, task
import json
import requests
import base64
import easyocr
import configparser
from collections import namedtuple


def on_locust_init(Person: dict):
    # read configuration file
    print(Person["username"])
    username = Person["username"]
    password = Person["password"]
    captcha_url = Person["captcha"]
    login_url = Person['login']
    order_url = Person['order']
    Person.pop("username")
    Person.pop("password")
    Person.pop("captcha")
    Person.pop("login")
    Person.pop("order")

    Person["validity"] = int(Person["validity"])
    Person["side"] = int(Person["side"])
    Person["accountType"] = int(Person["accounttype"])
    Person.pop("accounttype")
    Person["price"] = int(Person["price"])
    Person["volume"] = int(Person["volume"])
    Person["validityDate"] = None

    dictionary = json.dumps(Person)
    token = ""
    # decode captcha image

    def decoder(im):
        binary_data = base64.b64decode(im)
        reader = easyocr.Reader(['en'], gpu=False)
        result = reader.readtext(binary_data, detail=0)
        return "".join(result)

    # get captcha and login
    def get_captcha_and_login(username, password, captcha_url, login_url, decoder):
        print(username, password, captcha_url, login_url)
        response = requests.get(captcha_url)
        data = response.json()

        captcha_byte_data = data["captchaByteData"]
        salt = data["salt"]
        hashed_captcha = data["hashedCaptcha"]

        captcha = decoder(captcha_byte_data)

        data = {"loginName": username, "password": password, "captcha": {"hash": hashed_captcha,
                                                                         "salt": salt, "value": captcha}}
        response = requests.post(login_url, json=data)

        return response

    # get access token
    loginResponse = get_captcha_and_login(
        username, password, captcha_url, login_url, decoder)

    while not loginResponse.json().get("token"):
        loginResponse = get_captcha_and_login(
            username, password, captcha_url, login_url, decoder)

    token = loginResponse.json().get("token")
    print("login ok ! " + username + " " +
          login_url + "\n")
    result = namedtuple("ABC", "order token data")
    return result(order_url, token, dictionary)


class Mostafa_Ib(FastHttpUser):
    abstract = True

    def Populate(self, data: str, address: str, token: str):
        self.JsonData = data
        self.OrderAddress = address
        self.Token = token

    @ task
    def Mostafa_Ib_(self):

        self.client.request(method="Post",
                            url=self.OrderAddress,
                            name=self.fullname(),
                            data=self.JsonData,
                            headers={"authorization": f"Bearer {self.Token}",
                                     'Content-Type': 'application/json',
                                     'Accept': 'application/json',
                                     'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/102.0.5005.61/63 Safari/537.36'
                                     }
                            )


config = configparser.ConfigParser()
config.read('config.ini')
classes = []
for section_name in config.sections():
    section = dict(config[section_name])
    data = on_locust_init(section)
    globals()[section_name] = type(section_name, (Mostafa_Ib,), {})
    globals()[section_name].Populate(
        globals()[section_name], data.data, data.order, data.token)
    print(f"Section: {section_name}")
