from locust import FastHttpUser, task
import json
import requests
import configparser
from collections import namedtuple
from datetime import datetime, timedelta
import time

# API Endpoint
API_URL = "https://api-mts.orbis.easytrader.ir/easy/api/account/server-time/"
def on_locust_init(Person: dict):
    # read configuration file
    print(Person["username"])
    username = Person["username"]
    password = Person["password"]
    orderAddress = Person["order"]
    draftOrder = Person["draftorder"]
    batchOrder = Person["batchorder"]

    Person.pop("username")
    Person.pop("password")
    Person.pop("order")
    Person.pop("draftorder")
    Person.pop("batchorder")

    Person["validitytype"] = int(Person["validitytype"])
    Person["side"] = int(Person["side"])
    Person["price"] = int(Person["price"])
    Person["quantity"] = int(Person["quantity"])
    Person["validityDate"] = None

    dictionary = json.dumps(Person)

    def load_token_from_file(username):
        try:
            with open(f"{username}_Orbis.txt", "r") as file:
                token, timestamp = file.read().split('\n')
                token_time = datetime.fromisoformat(timestamp)
                print(token)
                if datetime.now() - token_time < timedelta(hours=2):
                    return token
        except (FileNotFoundError, ValueError):
            return None

    def get_server_time_offset(local_timestamp, access_token):
        # Call the API
        time_response = requests.get(f"{API_URL}{local_timestamp}", headers={"authorization": f"Bearer {access_token}"})
        time_response.raise_for_status()  # Raise error if the request fails
        json_data = time_response.json()

        # Calculate the offset
        diff = json_data["diff"]
        return diff

    def convert_server_time_to_local(server_time, offset):
        # Convert server time to local time using the offset
        local_time = server_time - offset
        return local_time

    token = load_token_from_file(username)
    if not token:
        raise Exception("Please save token and exit")

    print("login ok ! " + username + " " )
    # Prepare the payload
    payload = {
        "draft": {
            "symbolIsin": Person["symbolisin"],
            "symbolName": Person["symbolname"],
            "price": Person["price"],
            "quantity": Person["quantity"],
            "side": Person["side"],
            "validityType": Person["validitytype"],
            "validityDate": Person["validityDate"]
        }
    }

    # Send the POST request 2 times and save the responses
    responses = []
    for _ in range(2):
        response = requests.post(draftOrder, json=payload, headers={"authorization": f"Bearer {token}"})
        response_data = response.json()
        responses.append(response_data["id"])


    result = namedtuple("ABC", "order token data start end")
    url = batchOrder

    # Step 1: Get the initial offset
    local_timestamp = int(time.time() * 1000)  # Current time in milliseconds
    offset = get_server_time_offset(local_timestamp, token)

    # Step 2: Calculate when to send the request
    # Desired server time range: 8:44:58 to 8:45:02
    desired_server_time_start = datetime.strptime("08:44:43.500", "%H:%M:%S.%f").time()
    desired_server_time_end = datetime.strptime("08:45:00.500", "%H:%M:%S.%f").time()

    # Convert desired server time to local timestamp
    now = datetime.now()
    start_time = datetime.combine(now.date(), desired_server_time_start)
    end_time = datetime.combine(now.date(), desired_server_time_end)

    # Adjust using offset
    start_time_local = convert_server_time_to_local(int(start_time.timestamp() * 1000), offset)
    end_time_local = convert_server_time_to_local(int(end_time.timestamp() * 1000), offset)

    return result(url, token, responses, start_time_local, end_time_local)


class Mostafa_Ib(FastHttpUser):
    abstract = True

    def Populate(self, data: str, address: str, token: str, start: int, end : int):
        payload = {
            "draftIds": data,
            "removeDraftAfterCreate": False,
            "orderFrom": 34
        }
        self.JsonData = json.dumps(payload)
        self.OrderAddress =address
        self.Token = token
        self.Start = start
        self.End = end

    @ task
    def Mostafa_Ib_(self):

        current_time = time.time() * 1000  # Current time in milliseconds
        if self.Start <= current_time <= self.End:
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
config.read('config.orbis.ini')
classes = []
for section_name in config.sections():
    section = dict(config[section_name])
    data = on_locust_init(section)
    globals()[section_name] = type(section_name, (Mostafa_Ib,), {})
    globals()[section_name].Populate(
        globals()[section_name], data.data, data.order, data.token, data.start, data.end)
    print(f"Section: {section_name}")
