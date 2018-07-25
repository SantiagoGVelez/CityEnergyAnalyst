"""
Global variables - this object contains context information and is expected to be refactored away in future.
"""

# Economics data
INTEREST_RATE = 0.05  # 5% interest rate
ELECTRICITY_COST = 0.23  # SGD per kWh
THERMAL_COST = 0.04  # SGD per kWh

# Technical Data
V_BASE = 22.0  # in kV
S_BASE = 10.0  # in MVA
I_BASE = ((S_BASE/V_BASE) * (10**3))
APPROX_LOSS_HOURS = 3500
