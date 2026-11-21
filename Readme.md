 # Decentralized Smart Energy Grid with Peer-to-Peer Energy Trading

A hardware-in-the-loop (HIL) smart-grid framework for autonomous
peer-to-peer (P2P) electricity trading between residential prosumers and
consumers. The system combines IoT-based energy telemetry,
market-clearing algorithms, reinforcement learning, and a lightweight
blockchain ledger to enable local energy exchange with reduced
dependence on the utility grid.

## Overview

Traditional residential electricity systems often compensate
rooftop-solar prosumers at a low feed-in rate while charging consumers a
higher retail tariff. This project explores a decentralized local market
in which households can trade surplus electricity directly with nearby
consumers.

The proposed four-node microgrid uses ESP32 edge devices, MQTT
communication, a Python trading engine, and a React-based monitoring
dashboard. When local P2P supply cannot satisfy demand, the system uses
a Peer-to-Grid (P2G) fallback so that unmet demand can be served by the
utility grid.

The experimental evaluation described in the accompanying research
manuscript uses BESCOM tariff values and a 24-hour four-node microgrid
setup.

## Key Features

-   **Decentralized P2P energy trading:** Enables local electricity
    exchange between prosumers and consumers.
-   **Real-time IoT telemetry:** ESP32 nodes publish generation,
    consumption, battery, bidding, and balance information through MQTT.
-   **Dynamic market pricing:** Uses inverse-scarcity pricing and
    competitive ask formation.
-   **Fair market clearing:** Applies a reputation-weighted Nash
    Bargaining Solution (NBS).
-   **Adaptive pricing agents:** Uses Q-learning and short-term
    price-trend prediction to adjust agent strategies.
-   **Peer-to-Grid fallback:** Routes unmatched demand or surplus
    through the utility-grid interface.
-   **Tamper-evident transaction history:** Records trades in a SHA-256
    hash-chained ledger.
-   **Reputation mechanism:** Updates agent reputation according to
    trade-delivery outcomes.
-   **Live monitoring dashboard:** Displays trading activity,
    supply-demand ratio, energy flows, and battery state of charge.

## System Architecture

The framework is organized into four layers:

1.  **Physical / IoT Layer**
    -   Four residential nodes: two prosumers and two consumers.
    -   ESP32-WROOM-32 edge modules.
    -   DC bus and relay-based energy-routing demonstration.
    -   Battery state-of-charge tracking.
2.  **Telemetry and Communication Layer**
    -   Eclipse Mosquitto MQTT broker.
    -   MQTT over TCP for backend agents.
    -   MQTT over WebSockets for browser-based dashboard updates.
    -   Periodic publication of node telemetry and market events.
3.  **Algorithmic Market-Clearing Layer**
    -   Solar-generation and appliance-load inputs.
    -   Supply-demand ratio and inverse-scarcity pricing.
    -   Bertrand-style competitive ask formation.
    -   Reputation-weighted NBS clearing.
    -   Q-learning price adaptation and price-trend lookahead.
    -   P2G fallback for unmatched positions.
4.  **Security and Visualization Layer**
    -   SHA-256 hash-chained transaction ledger.
    -   Trade and delivery-status records.
    -   React dashboard with live market and energy visualizations.

## Algorithms and Models

### 1. Inverse-Scarcity Pricing

The system derives a scarcity signal from aggregate supply and demand.
The resulting price moves within the utility tariff band: surplus supply
pushes the price toward the feed-in floor, while scarcity moves it
toward the retail ceiling.

### 2. Nash Bargaining Solution

The NBS determines a mutually beneficial clearing price between a seller
and buyer. Bargaining weights incorporate battery state of charge and
agent reputation.

### 3. Bertrand Competitive Ask Formation

Prosumer agents adjust their asking prices relative to competing
sellers, using an undercutting step to encourage competitive offers.

### 4. Q-Learning

Each node maintains a Q-table with states based on scarcity and battery
state-of-charge zones. Available actions nudge the agent's price up,
down, or leave it unchanged. The reward function accounts for trading
outcomes and battery depletion.

### 5. Price-Trend Prediction

A rolling history of recent clearing prices is used for local
linear-regression-based, one-step price prediction. The prediction helps
agents decide whether to raise, lower, or hold their prices.

### 6. Reputation Update

Agent reputation is updated after trade delivery. Successful delivery
improves reputation, while failed delivery reduces it. Reputation
influences bargaining power in subsequent NBS rounds.

### 7. Blockchain Ledger

Each trade is appended to a SHA-256 hash-chained ledger. Recorded
information includes participant identifiers, bids and asks, clearing
price, traded volume, cost, battery state of charge, reputation, and
delivery status.

## Technology Stack

  Component                  Technology
  -------------------------- -----------------------------
  Edge controllers           ESP32-WROOM-32
  Central simulation host    Raspberry Pi 4
  Backend / trading engine   Python 3.11
  Messaging                  Eclipse Mosquitto, MQTT
  Python MQTT client         Paho-MQTT
  Dashboard                  React / HTML and JavaScript
  Browser messaging          MQTT.js over WebSockets
  Ledger hashing             Python `hashlib`, SHA-256
  Agent learning and data    Q-learning, NumPy
  Solar data source          NASA POWER API

## Experimental Setup

The manuscript describes a four-node microgrid:

  Node      Role
  --------- ----------
  House 1   Prosumer
  House 2   Prosumer
  House 3   Consumer
  House 4   Consumer

The reported evaluation covers a 24-hour operating period, with results
averaged over five independent runs. The tariff values used in the
experiments are:

-   Grid buy price: **₹9.20/kWh**
-   Grid sell / feed-in price: **₹4.50/kWh**

The physical demonstration uses ESP32 edge nodes, a Raspberry Pi host,
MQTT communication, and relay-based routing on a low-voltage DC setup.

## Reported Results

The following values are reported in the accompanying manuscript for the
evaluated four-node setup:

  Metric                                 Peak solar period   Full-day average
  ------------------------------------ ------------------- ------------------
  P2P share of total demand                            61%                38%
  NBS clearing price                      ₹7.10--₹7.60/kWh          ₹7.95/kWh
  Grid-import reduction vs. baseline                   43%                28%
  MQTT tick latency                               \< 10 ms           \< 10 ms
  Blockchain block rate                    \~18 blocks/min     \~7 blocks/min
  Trade delivery success                             98.2%              98.1%
  Average agent reputation                           0.982              0.981

These are results reported by the research manuscript, not guaranteed
performance figures for every deployment or hardware configuration.

## Getting Started

This README summarizes the architecture and technologies documented in
the research manuscript. The manuscript does not specify the exact
repository directory structure, dependency lockfile,
environment-variable names, or executable entry-point commands. Update
this section to match the actual source code before publishing the
repository.

### Prerequisites

The documented implementation uses:

-   Python 3.11
-   Eclipse Mosquitto MQTT broker
-   Paho-MQTT
-   NumPy
-   A React / JavaScript-compatible environment for the dashboard
-   MQTT.js for browser communication
-   Raspberry Pi 4 and ESP32 hardware for the hardware-in-the-loop setup

### High-Level Setup

1.  Install Python 3.11 and the Python packages required by the trading
    engine.
2.  Install and configure Eclipse Mosquitto, including the MQTT TCP
    listener and WebSocket listener used by the backend and dashboard.
3.  Configure the four node identities and their generation,
    consumption, battery, and trading parameters.
4.  Configure access to the NASA POWER solar-irradiance data source,
    along with the documented fallback behavior.
5.  Connect the ESP32 nodes to the MQTT broker, or use the project's
    simulation inputs if running without physical hardware.
6.  Configure and start the dashboard so it can subscribe to the
    broker's WebSocket endpoint.
7.  Start the trading engine using the entry point provided by the
    repository and verify that telemetry, trades, and ledger entries are
    being produced.

> **Note:** Add the exact installation commands, configuration file
> names, broker addresses, and run commands once they have been verified
> against the repository. Do not expose passwords, API credentials, or
> other secrets in committed files.

## Dashboard

The monitoring interface is designed to present:

-   Live transaction feed
-   Supply-demand ratio (SDR) trends
-   Trades versus time
-   P2P and grid energy exchange
-   Energy-flow information
-   Battery state-of-charge indicators

## Limitations and Future Work

The manuscript identifies several areas for further development:

-   Move from simultaneous periodic trading ticks to a continuous-time
    order-matching engine.
-   Extend the simplified DC-bus model to include AC power flow,
    reactive power, phase imbalance, and network constraints.
-   Investigate Deep Q-Networks and multi-agent reinforcement learning
    for larger state spaces and more agents.
-   Scale the HIL setup to larger microgrids and more physical edge
    nodes.
-   Improve resilience testing under grid faults, communication loss,
    and emergency operating conditions.
-   Explore EVM-compatible smart-contract settlement and more advanced
    edge-meter integration.

## Research Context

This project is documented in the research manuscript:

**"Decentralized Smart Energy Grid with Peer-to-Peer Energy Trading."**

The work investigates the integration of local energy markets, IoT
telemetry, reinforcement learning, and a lightweight cryptographic
ledger for residential microgrids.

## Contributors

-   **Rohan Satish Dodwad**
-   **MD Suraj**


## Acknowledgement

The project is based on the research and implementation details
described in the accompanying manuscript. Please refer to the manuscript
for the full mathematical formulation, methodology, figures,
experimental discussion, and references.

## License

No license information was specified in the supplied manuscript. Add a
license (for example, MIT) only after the project owners have selected
one.
