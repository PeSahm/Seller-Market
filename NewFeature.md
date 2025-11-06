# Improve Seller market pp

I want to eliminate some manual steps in this app . as you can see here is current configuration sample:

    [Order_Mostafa_Mehregan_GS]
    username = 4580090306
    password = Mm@12345
    captcha = https://identity-gs.ephoenix.ir/api/Captcha/GetCaptcha 
    login = https://identity-gs.ephoenix.ir/api/v2/accounts/login
    order = https://api-gs.ephoenix.ir/api/v2/orders/NewOrder
    validity = 1
    side = 1
    accounttype = 1
    price = 5860
    volume = 170017
    isin = IRO1MHRN0001
    serialnumber = 0
    editorder = https://api-gs.ephoenix.ir/api/v2/orders/EditOrder

i have to manualyy enter price and volume every day based on **isin**(the symbole identifire) and based on my **Buying Power**.

1- The configuration can be simpler and more dynamic. As you know we can have muliple configuration inf config.ini file to be able to send request to multiple broker or fo multiple symbol.
ّFirst I need enum for brokers
Ganjine -> gs
Shahr -> sharr
BourseBime => bbه
and so on. i will update it my self
So the address is like :

    captcha = https://identity-{brokerCode}.ephoenix.ir/api/Captcha/GetCaptcha 
    login = https://identity-{brokerCode}.ephoenix.ir/api/v2/accounts/login
    order = https://api-{brokerCode}.ephoenix.ir/api/v2/orders/NewOrder
    editorder = https://api-{brokerCode}.ephoenix.ir/api/v2/orders/EditOrder

 So config can be simpler by getting broker enum.
 
 these two options are fixes so can be hardcoded

     validity = 1
     accounttype = 1

it's a personal project so i have no worries about credential
keep these two items as it is . 

     serialnumber = 0
     editorder = https://api-gs.ephoenix.ir/api/v2/orders/EditOrder

First of all the login flow and captcha flow works well. so it should be as the first step of all intractions, we need to get jwt token for all commination with the system.

We should request to fetch current Buying Power of current user
This is a Get http request. jwt token should be in header as bearer token
https://api-{brokerCode}.ephoenix.ir/api/v2/tradingbook/GetLastTradingBook

The response is :
  

    {"buyingPower": 1000014598,"credit": 0,"remain": 1000014598,"stockRemain": 999997885,"blockRemain": 0,"stockBlock": 0,"onlineBlock": 0,"marginBlock": 0,"futureMarginBlock": 0,"settlementBlock": 0,"optionPower": 1000014598,"optionRemainT2": 16713,"optionBlockRemain": 0,"optionOrderBlock": 0,"optionCredit": 0,"futureSettlementBlock": 0,"futureDailyLossBlock": 0,"cashFlowBlock": 0,"pamCode": "17894580090306","equityBuyTrade": 0,"equitySellTrade": 0,"limitedOptionCredit": true,"buyingPowerT1": 1000014598,"isSellVIP": false,"minimumRequiredAmount": 0,"accountStatus": 0,"accountStatusDescrp": "عادی","timestamp": 1762376255.9292984}

we need to the **buyingPower** property of the response.
We need it to calcute the order volume.
To achive that we need more data from the symbol:
There is an api called **full** . Again authentication header is a must.
the curel request : 

    curl 'https://mdapi1.ephoenix.ir/api/v2/instruments/full' \
      -H 'sec-ch-ua-platform: "Windows"' \
      -H 'Referer: https://bbi.ephoenix.ir/' \
      -H 'sec-ch-ua: "Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"' \
      -H 'sec-ch-ua-mobile: ?0' \
      -H 'x-sessionId: OMS5eff5322-3987-4cc0-9cfc-f4e019f488b8' \
      -H 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36' \
      -H 'Accept: application/json, text/plain, */*' \
      -H 'DNT: 1' \
      -H 'Content-Type: application/json' \
      --data-raw '{"isinList":["IRO1BORS0001"]}'
you only need to chenge the **isinList** based on the configuration.

The response :

    [
        {
            "i": {
                "isin": "IRO1BORS0001",
                "cIsin": "",
                "t": "بورس اوراق بهادار تهران",
                "s": "بورس",
                "sgt": "",
                "mt": "",
                "se": "67",
                "ses": "6711",
                "set": "اداره بازارهای مالی",
                "sest": "اداره ی بازارهای مالی",
                "b": "1",
                "bt": "بازار اول (تابلوی اصلی)",
                "cc": "BORS",
                "ncs": "A",
                "ncst": "مجاز",
                "ic": "60523145697836739",
                "mc": 24000000000,
                "ftp": 3393.00,
                "bav": 9600000,
                "lcp": 3497.00,
                "cp": 3395.00,
                "cpc": -102.00,
                "cpcp": -2.92,
                "cd": "2025-11-05T08:59:59Z",
                "ct": "بورس اوراق بهادار تهران",
                "ex": "1",
                "ext": "بورس",
                "exnt": "TSE",
                "isDeleted": false,
                "pe": 20.09,
                "eps": 169,
                "mineq": 1,
                "maxeq": 400000,
                "minebq": 400000,
                "maxesq": 400000,
                "ls": 1,
                "wt": 1,
                "ag": [
                    3
                ],
                "nav": 0,
                "navd": "",
                "cusc": 0,
                "cus": "عادی",
                "cur": "",
                "optionContract": {
                    "id": null,
                    "companyIsin": "",
                    "openPositions": 0,
                    "initialMargin": 0,
                    "maintenanceMargin": 0,
                    "requiredMargin": 0,
                    "strikePrice": 0,
                    "contractSize": 0,
                    "startDate": "0001-01-01T00:00:00",
                    "endDate": "0001-01-01T00:00:00",
                    "cashSettlementDate": "0001-01-01T00:00:00",
                    "physicalSettlementDate": "0001-01-01T00:00:00",
                    "maxPositionForRealPerson": 0,
                    "maxPositionForLegalPerson": 0,
                    "maxBrokerPosition": 0,
                    "maxMarketPosition": 0,
                    "maxOrders": 0,
                    "baseCompanyIsin": "",
                    "cefo": false,
                    "lastUpdateTime": "0001-01-01T00:00:00",
                    "baseIsin": "",
                    "daysTillSettlement": 0
                },
                "futureContract": {
                    "id": null,
                    "companyIsin": "",
                    "baseCompanyIsin": "",
                    "initialMarginCoefficient": 0,
                    "requiredMarginCoefficient": 0,
                    "minimumMarginCoefficient": 0,
                    "roundCoefficient": 0,
                    "extraMargin": 0,
                    "contractSize": 0,
                    "startDate": "0001-01-01T00:00:00",
                    "endDate": "0001-01-01T00:00:00",
                    "maxRealPersonPosition": 0,
                    "maxLegalPersonPosition": 0,
                    "maxMarketPosition": 0,
                    "marketPosition": 0,
                    "cashSettlementWageCoefficient": 0,
                    "physicalSettlementWageCoefficient": 0,
                    "penaltyCoefficient": 0,
                    "cashSettlementDate": "0001-01-01T00:00:00",
                    "physicalSettlementDate": "0001-01-01T00:00:00",
                    "baseIsin": "",
                    "daysTillSettlement": 0
                },
                "aofpt": 1.00,
                "tmaxap": 3496.00,
                "tminap": 3294.00,
                "etfTypeDescription": "",
                "etfType": 0,
                "isUnusedRight": false,
                "isOp": false
            },
            "t": {
                "isin": "IRO1BORS0001",
                "maxap": 3601.00,
                "minap": 3393.00,
                "z": 3497.00,
                "cup": 3417.00,
                "lp": 3393.00,
                "hp": 3430.00,
                "cd": "2025-11-05T08:59:59Z",
                "cupc": -80.00,
                "cupcp": -2.29,
                "tnt": 1035,
                "tnst": 54046928,
                "ttv": 183464812785.00,
                "lpcp": -2.97,
                "hpcp": -1.92,
                "hpc": -67,
                "lpc": -104
            },
            "bl": [
                {
                    "isin": "IRO1BORS0001",
                    "bv": 770804,
                    "boc": 10,
                    "bp": 3393.00,
                    "sv": 35000,
                    "soc": 1,
                    "sp": 3421.00,
                    "r": 1,
                    "ca": "2025-11-05T09:11:41Z",
                    "bvp": 100,
                    "svp": 31.88,
                    "bpv": true,
                    "spv": true
                },
                {
                    "isin": "IRO1BORS0001",
                    "bv": 0,
                    "boc": 0,
                    "bp": 0,
                    "sv": 11926,
                    "soc": 2,
                    "sp": 3427.00,
                    "r": 2,
                    "ca": "2025-11-05T09:01:09Z",
                    "bvp": 0,
                    "svp": 10.86,
                    "bpv": false,
                    "spv": true
                },
                {
                    "isin": "IRO1BORS0001",
                    "bv": 0,
                    "boc": 0,
                    "bp": 0,
                    "sv": 47874,
                    "soc": 1,
                    "sp": 3429.00,
                    "r": 3,
                    "ca": "2025-11-05T10:21:06Z",
                    "bvp": 0,
                    "svp": 43.6,
                    "bpv": false,
                    "spv": true
                },
                {
                    "isin": "IRO1BORS0001",
                    "bv": 0,
                    "boc": 0,
                    "bp": 0,
                    "sv": 9992,
                    "soc": 1,
                    "sp": 3430.00,
                    "r": 4,
                    "ca": "2025-11-05T10:21:06Z",
                    "bvp": 0,
                    "svp": 9.1,
                    "bpv": false,
                    "spv": true
                },
                {
                    "isin": "IRO1BORS0001",
                    "bv": 0,
                    "boc": 0,
                    "bp": 0,
                    "sv": 5000,
                    "soc": 1,
                    "sp": 3440.00,
                    "r": 5,
                    "ca": "2025-11-05T10:21:06Z",
                    "bvp": 0,
                    "svp": 4.55,
                    "bpv": false,
                    "spv": true
                },
                {
                    "isin": "IRO1BORS0001",
                    "bv": 0,
                    "boc": 0,
                    "bp": 0,
                    "sv": 0,
                    "soc": 0,
                    "sp": 0,
                    "r": 6,
                    "ca": "2025-11-05T02:31:24Z",
                    "bvp": 0,
                    "svp": 0,
                    "bpv": false,
                    "spv": false
                }
            ],
            "rlt": {
                "isin": "IRO1BORS0001",
                "rsv": 49131614,
                "rsc": 289,
                "rbv": 29164666,
                "rbc": 370,
                "lsv": 4915314,
                "lsc": 2,
                "lbv": 24882262,
                "lbc": 3,
                "lbp": 46.04,
                "lsp": 9.09,
                "rsp": 90.91,
                "rbp": 53.96,
                "realPercentage": 0,
                "legalPercentage": 0
            },
            "ocd": {
                "theoreticalPrice": 0,
                "delta": 0,
                "gamma": 0,
                "vega": 0,
                "theta": 0,
                "rho": 0,
                "impliedVolatility": 0,
                "historicalVolatility": 0,
                "leverage": 0,
                "breakEvenPoint": 0,
                "breakEvenPointPercent": 0,
                "spread": 0,
                "intrinsicValue": 0,
                "timeValue": 0,
                "optionContractStatus": 0
            }
        }
    ]
What is important from this resposne?

 - The response containa a **t** inner object and the object has **maxap** that is a upper allowd treshold in a day. for side = 1 (buy) the price should be **maxap**
and for side = 2 the price should be : **minap**
 - The response contains a **i** inner object and object has **maxeq** . it's the maximum allowd order valume. it mean that if we have a high buying power be cannot set everything as order volume, I tell you how calculte that but keep it in mind that we should alwase comaper the calculated volumne with this one and it should not exeeted for it .

There is a calculator api that calculate order volume based on buying power
the request:

    curl 'https://api-{brokerCode}.ephoenix.ir/api/v2/orders/CalculateOrderParam' \
      -H 'sec-ch-ua-platform: "Windows"' \
      -H 'Referer: https://bbi.ephoenix.ir/' \
      -H 'sec-ch-ua: "Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"' \
      -H 'sec-ch-ua-mobile: ?0' \
      -H 'x-sessionId: OMS5eff5322-3987-4cc0-9cfc-f4e019f488b8' \
      -H 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36' \
      -H 'Accept: application/json, text/plain, */*' \
      -H 'DNT: 1' \
      -H 'Content-Type: application/json' \
      --data-raw '{"isin":"IRO1BORS0001","side":1,"totalNetAmount":1000014598,"price":3601}' 
as you see body is object that contais **isin**, side and totalNetAmount and price
isin is from config
side is from config
totalNetAmount  is the **BuyingPower** we fetech that before
price is ther order price that we fetched that before
The response is :

    {"volume":276677,"totalNetAmount":1000014598.0,"totalFee":3698325.0}

The thing that we need in **volume** .It's the thing that should be compared with the maximum allowd volume and then put as order volume.

Locust has some event hooks like on_test_stop

I want to use use this to query about the order result :
you should call :

    curl 'https://api-{brokerCode}.ephoenix.ir/api/v2/orders/GetOpenOrders?type=1' \
      -H 'sec-ch-ua-platform: "Windows"' \
      -H 'Referer: https://bbi.ephoenix.ir/' \
      -H 'sec-ch-ua: "Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"' \
      -H 'sec-ch-ua-mobile: ?0' \
      -H 'x-sessionId: OMS5eff5322-3987-4cc0-9cfc-f4e019f488b8' \
      -H 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36' \
      -H 'Accept: application/json, text/plain, */*' \
      -H 'DNT: 1'
```json
[
  {
    "isin": "string",
    "traderId": "string",
    "orderSide": 1,
    "created": "2025-11-05T21:22:42.048Z",
    "modified": "2025-11-05T21:22:42.048Z",
    "modifiedShamsiDate": "string",
    "createdShamsiDate": "string",
    "volume": 0,
    "remainedVolume": 0,
    "netAmount": 0,
    "trackingNumber": 0,
    "serialNumber": 0,
    "price": 0,
    "state": 1,
    "stateDesc": "string",
    "pamCode": "string",
    "replyTime": "2025-11-05T21:22:42.048Z",
    "validUntil": "2025-11-05T21:22:42.048Z",
    "isLocked": true,
    "userId": 0,
    "symbol": "string",
    "executedVolume": 0,
    "canceledVolume": 0,
    "executedAmount": 0,
    "blockedAmount": 0,
    "executedBlockedAmount": 0,
    "isDone": true,
    "validity": 1,
    "validityTypeDesc": "string",
    "jalaliValidUntil": "string",
    "prevValidity": 0,
    "prevValidUntil": "2025-11-05T21:22:42.048Z",
    "prevVolume": 0,
    "prevRemainedVolume": 0,
    "prevPrice": 0,
    "newVolume": 0,
    "newPrice": 0,
    "traderUserId": 0,
    "crossPamCode": "string",
    "symbolTitle": "string",
    "crossUserId": 0,
    "crossNetAmount": 0,
    "stateDescription": "string",
    "netTradedValue": 0
  }
]
```
You should save the result for each user and each day in a file 
createdShamsiDate and isin and trackingNumber should be saved.



I need great logging that can help me to check and debug.
also good to have unit test to simulate the flow.


