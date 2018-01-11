"""
This is the dashboard of CEA
"""
from __future__ import division
from __future__ import print_function

import os

import pandas as pd

import cea.config
import cea.inputlocator
from cea.plots.building.energy_use_intensity import energy_use_intensity
from cea.plots.building.heating_reset_schedule import heating_reset_schedule
from cea.plots.building.load_curve import load_curve
from cea.plots.building.load_duration_curve import load_duration_curve
from cea.plots.building.peak_load import peak_load_stacked
from cea.utilities import epwreader

__author__ = "Jimeno A. Fonseca"
__copyright__ = "Copyright 2018, Architecture and Building Systems - ETH Zurich"
__credits__ = ["Jimeno A. Fonseca"]
__license__ = "MIT"
__version__ = "0.1"
__maintainer__ = "Daren Thomas"
__email__ = "cea@arch.ethz.ch"
__status__ = "Production"

def aggregate(analysis_fields, buildings, locator):
    for i, building in enumerate(buildings):
        if i == 0:
            df = pd.read_csv(locator.get_demand_results_file(building))
        else:
            df2 = pd.read_csv(locator.get_demand_results_file(building))
            for field in analysis_fields:
                df[field] = df[field].values + df2[field].values
    return df


def dashboard(locator, config):
    # GET LOCAL VARIABLES
    buildings = []#["B05","B03", "B01", "B04", "B06"]

    if buildings == []:
        buildings = pd.read_csv(locator.get_total_demand()).Name.values

    # CREATE LOAD DURATION CURVE
    output_path = locator.get_timeseries_plots_file("District" + '_load_duration_curve')
    title = "Load Duration Curve for District"
    analysis_fields = ["Ef_kWh", "Qhsf_kWh", "Qwwf_kWh", "Qcsf_kWh"]
    df = aggregate(analysis_fields, buildings, locator)
    load_duration_curve(df, analysis_fields, title, output_path)

    # CREATE LOAD CURVE
    output_path = locator.get_timeseries_plots_file("District" + '_load_curve')
    title = "Load Curve for District"
    # GET LOCAL WEATHER CONDITIONS
    weather_data = epwreader.epw_reader(config.weather)[["drybulb_C", "wetbulb_C", "skytemp_C"]]
    df["T_out_dry_C"] = weather_data["drybulb_C"].values
    analysis_fields = ["Ef_kWh", "Qhsf_kWh", "Qwwf_kWh", "Qcsf_kWh", "T_int_C", "T_out_dry_C"]
    load_curve(df, analysis_fields, title, output_path)

    # CREATE PEAK LOAD STACKED
    df2 = pd.read_csv(locator.get_total_demand()).set_index("Name")
    output_path = locator.get_timeseries_plots_file("District" + '_peak_load')
    title = "Peak load for District"
    analysis_fields_loads = ["Ef_kWh", "Qhsf_kWh", "Qwwf_kWh", "Qcsf_kWh"]
    analysis_fields_peaks = ["Ef0_kW", "Qhsf0_kW", "Qwwf0_kW", "Qcsf0_kW"]
    peak_load_district(df, analysis_fields_loads, anlaysis_fields_peaks title, output_path)






def main(config):
    assert os.path.exists(config.scenario), 'Scenario not found: %s' % config.scenario
    locator = cea.inputlocator.InputLocator(config.scenario)

    # print out all configuration variables used by this script
    print("Running dashboard with scenario = %s" % config.scenario)

    dashboard(locator, config)


if __name__ == '__main__':
    main(cea.config.Configuration())
