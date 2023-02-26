
# Stock Market Seller's Market Simulation with Python and Locust 

This repository contains a Python code for simulating a seller's market situation in a stock market using Locust. The purpose of this code is to help test and evaluate the performance and scalability of stock market trading systems and algorithms in a seller's market scenario, where there are more sellers than buyers.
 ## Requirements
  To run this code, you need to have Python 3.x and Locust installed in your system. You can install Locust via pip:
 ```bash 
 pip install locust
 ```
 ## Usage
 To use this code, clone this repository and run the `locustfile.py` script with Locust:
 ```bash
 `locust -f locustfile.py`
 ```
 This will start the Locust web interface at `http://localhost:8089/`, where you can configure the number of users, spawn rate, and other parameters of the simulation. You can also monitor the performance and results of the simulation in real-time using the web interface.
## Features
The code includes the following features and functionalities:

-   **Seller's market simulation:** The simulation creates a seller's market scenario, where there are more sellers than buyers, and the prices tend to decrease.
-   **Random orders generation:** The simulation generates random orders with different prices and quantities, following a normal distribution with configurable mean and standard deviation parameters.
-   **Order execution logic:** The simulation executes the orders following a simple matching logic, where the highest price seller's orders are matched with the lowest price buyer's orders, and vice versa, until all orders are executed or there are no more matching orders.
-   **Metrics and statistics:** The simulation collects and reports various metrics and statistics, such as the number of orders, the execution time, the latency, the response time, the number of failures, etc.
## Contributing

## Tasks

 - [x] Sahra online trading systems
 - [ ] Mofid Securities Orbis trader
 - [ ] Rayan online trading system (Exir)
 - [ ] Agah online trading system

If you want to contribute to this project, feel free to fork this repository and submit pull requests with your changes and improvements. You can also open issues or suggest new features and functionalities.

## License

This code is released under the [MIT License](https://chat.openai.com/LICENSE). Feel free to use and modify this code for your own purposes, as long as you include the original license and attribution.
