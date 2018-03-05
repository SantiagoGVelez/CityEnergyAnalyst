from __future__ import print_function

"""
============================
Hydraulic - thermal network
============================
"""

from __future__ import division
import time
import numpy as np
import pandas as pd
import cea.technologies.substation_matrix as substation
import math
from cea.utilities import epwreader
from cea.resources import geothermal
import geopandas as gpd
import cea.config
import cea.globalvar
import cea.inputlocator
import os
import random
import networkx as nx

__author__ = "Martin Mosteiro Romero, Shanshan Hsieh"
__copyright__ = "Copyright 2016, Architecture and Building Systems - ETH Zurich"
__credits__ = ["Martin Mosteiro Romero", "Shanshan Hsieh", "Lennart Rogenhofer"]
__license__ = "MIT"
__version__ = "0.1"
__maintainer__ = "Daren Thomas"
__email__ = "thomas@arch.ethz.ch"
__status__ = "Production"


def thermal_network_main(locator, gv, network_type, network_name, source, set_diameter):
    """
    This function performs thermal and hydraulic calculation of a "well-defined" network, namely, the plant/consumer
    substations, piping routes and the pipe properties (length/diameter/heat transfer coefficient) are already 
    specified.

    The hydraulic calculation is based on Oppelt, T., et al., 2016 for the case with no loops. Firstly, the consumer
    substation heat exchanger designs are calculated according to the consumer demands at each substation. Secondly,
    the piping network is imported as a node-edge matrix (NxE), which indicates the connections of all nodes and edges
    and the direction of flow between them following graph theory. Nodes represent points in the network, which could
    be the consumers, plants or joint points. Edges represent the pipes in the network. For example, (n1,e1) = 1 denotes
    the flow enters edge "e1" at node "n1", while when (n2,e2) = -1 denotes the flow leave edge "e2" at node "n2".
    Following, a steady-state hydraulic calculation is carried out at each time-step to solve for the edge mass flow
    rates according to mass conservation equations. With the maximum mass flow calculated from each edge, the property
    of each pipe is assigned.

    Thirdly, the hydraulic thermal calculation for each time-steps over a year is based on a heat balance for each
    edge (heat at the pipe inlet equals heat at the outlet minus heat losses through the pipe). Finally, the pressure
    loss calculation is carried out based on Todini et al. (1987)

    :param locator: an InputLocator instance set to the scenario to work on
    :param gv: an instance of globalvar.GlobalVariables with the constants  to use (like `list_uses` etc.)
    :param network_type: a string that defines whether the network is a district heating ('DH') or cooling ('DC')
                         network
    :param source: string that defines the type of source file for the network to be imported ('csv' or shapefile 'shp')

    :type locator: InputLocator
    :type gv: GlobalVariables
    :type network_type: str
    :type source: str

    The following files are created by this script, depending on the network type defined in the inputs:

    - DH_EdgeNode or DC_EdgeNode: .csv, edge-node matrix for the defined network
    - DH_AllNodes or DC_AllNodes: .csv, list of plant nodes and consumer nodes and their corresponding building names
    - DH_MassFlow or DC_MassFlow: .csv, mass flow rates at each edge for each time step
    - DH_T_Supply or DC_T_Supply: .csv, describes the supply temperatures at each node at each type step
    - DH_T_Return or DC_T_Return: .csv, describes the return temperatures at each node at each type step
    - DH_Plant_heat_requirement or DC_Plant_heat_requirement: .csv, heat requirement from the plants in a district
      heating or cooling network
    - DH_P_Supply or DC_P_Supply: .csv, supply side pressure for each node in a district heating or cooling network at
      each time step
    - DH_P_Return or DC_P_Return: .csv, return side pressure for each node in a district heating or cooling network at
      each time step
    - DH_P_DeltaP or DC_P_DeltaP.csv, pressure drop over an entire district heating or cooling network at each time step

    .. [Todini & Pilati, 1987] Todini & Pilati. "A gradient method for the analysis of pipe networks," in Computer
       Applications in Water Supply Volume 1 - Systems Analysis and Simulation, 1987.

    .. [Oppelt, T., et al., 2016] Oppelt, T., et al. Dynamic thermo-hydraulic model of district cooling networks.
       Applied Thermal Engineering, 2016.

    .. [Ikonen, E., et al, 2016] Ikonen, E., et al. Examination of Operational Optimization at Kemi District Heating
       Network. Thermal Science. 2016, Vol. 20, No.2, pp.667-678.
    """

    # # prepare data for calculation

    # read building names from the entire district
    building_names = pd.read_csv(locator.get_total_demand())['Name'].values

    # get edge-node matrix from defined network, the input formats are either .csv or .shp
    if source == 'csv':
        edge_node_df, all_nodes_df, edge_df = get_thermal_network_from_csv(locator, network_type, network_name)
    else:
        edge_node_df, all_nodes_df, edge_df, building_names = get_thermal_network_from_shapefile(locator, network_type,
                                                                                                 network_name)

    # calculate ground temperature
    weather_file = locator.get_default_weather()
    T_ambient_C = epwreader.epw_reader(weather_file)['drybulb_C']
    network_depth_m = gv.NetworkDepth  # [m]
    T_ground_K = geothermal.calc_ground_temperature(locator, T_ambient_C.values, network_depth_m)

    # substation HEX design
    substations_HEX_specs, buildings_demands = substation.substation_HEX_design_main(locator, building_names, gv)

    # get hourly heat requirement and target supply temperature from each substation
    t_target_supply_C = read_properties_from_buildings(building_names, buildings_demands,
                                                       'T_sup_target_' + network_type)
    t_target_supply_df = write_substation_temperatures_to_nodes_df(all_nodes_df, t_target_supply_C)  # (1 x n)

    ## assign pipe properties
    # calculate maximum edge mass flow
    edge_mass_flow_df_kgs, max_edge_mass_flow_df_kgs, pipe_properties_df = calc_max_edge_flowrate(all_nodes_df,
                                                                                                  building_names,
                                                                                                  buildings_demands,
                                                                                                  edge_node_df, gv,
                                                                                                  locator,
                                                                                                  substations_HEX_specs,
                                                                                                  t_target_supply_C,
                                                                                                  network_type,
                                                                                                  network_name, edge_df[
                                                                                                      'pipe length'],
                                                                                                  edge_df, set_diameter)

    # merge pipe properties to edge_df and then output as .csv
    edge_df = edge_df.merge(pipe_properties_df.T, left_index=True, right_index=True)
    edge_df.to_csv(locator.get_optimization_network_edge_list_file(network_type, network_name))

    ## Start solving hydraulic and thermal equations at each time-step
    t0 = time.clock()
    # create empty lists to write results
    T_return_nodes_list = []
    T_supply_nodes_list = []
    q_loss_supply_edges_list = []
    plant_heat_requirements = []
    pressure_nodes_supply = []
    pressure_nodes_return = []
    pressure_loss_system = []

    for t in range(8760):
        print('calculating thermal hydraulic properties of', network_type, 'network', network_name,
              '...  time step', t)
        timer = time.clock()

        ## solve network temperatures
        T_supply_nodes_K, \
        T_return_nodes_K, \
        plant_heat_requirement_kW, \
        edge_mass_flow_df_kgs.ix[t], \
        q_loss_supply_edges_kW = solve_network_temperatures(locator, gv, T_ground_K, edge_node_df, all_nodes_df,
                                                            edge_mass_flow_df_kgs.ix[t], t_target_supply_df,
                                                            building_names, buildings_demands, substations_HEX_specs,
                                                            t, network_type, network_name, edge_df, pipe_properties_df)

        # calculate pressure at each node and pressure drop throughout the entire network
        P_supply_nodes_Pa, P_return_nodes_Pa, delta_P_network_Pa = calc_pressure_nodes(edge_node_df,
                                                                                       pipe_properties_df[:][
                                                                                       'D_int_m':'D_int_m'].
                                                                                       values,
                                                                                       edge_df['pipe length'].values,
                                                                                       edge_mass_flow_df_kgs.ix[
                                                                                           t].values,
                                                                                       T_supply_nodes_K,
                                                                                       T_return_nodes_K, gv)

        # store node temperatures and pressures, as well as plant heat requirement and overall pressure drop at each
        # time step
        T_supply_nodes_list.append(T_supply_nodes_K)
        T_return_nodes_list.append(T_return_nodes_K)
        q_loss_supply_edges_list.append(q_loss_supply_edges_kW)
        plant_heat_requirements.append(plant_heat_requirement_kW)
        pressure_nodes_supply.append(P_supply_nodes_Pa[0])
        pressure_nodes_return.append(P_return_nodes_Pa[0])
        pressure_loss_system.append(delta_P_network_Pa)

        # print(time.clock() - timer, 'seconds process time for time step', t)

    # save results
    # edge flow rates (flow direction corresponding to edge_node_df)
    pd.DataFrame(edge_mass_flow_df_kgs, columns=edge_node_df.columns).to_csv(
        locator.get_optimization_network_layout_massflow_file(network_type, network_name),
        na_rep='NaN', index=False, float_format='%.3f')
    # node temperatures
    pd.DataFrame(T_supply_nodes_list, columns=edge_node_df.index).to_csv(
        locator.get_optimization_network_layout_supply_temperature_file(network_type, network_name),
        na_rep='NaN', index=False, float_format='%.3f')
    pd.DataFrame(T_return_nodes_list, columns=edge_node_df.index).to_csv(
        locator.get_optimization_network_layout_return_temperature_file(network_type, network_name),
        na_rep='NaN', index=False, float_format='%.3f')

    # save edge heat losses in the supply line
    pd.DataFrame(q_loss_supply_edges_list, columns=edge_node_df.columns).to_csv(
        locator.get_optimization_network_layout_qloss_file(network_type, network_name),
        na_rep='NaN', index=False, float_format='%.3f')

    # plant heat requirements
    pd.DataFrame(plant_heat_requirements,
                 columns=filter(None, all_nodes_df[all_nodes_df.Type == 'PLANT'].Building.values)).to_csv(
        locator.get_optimization_network_layout_plant_heat_requirement_file(network_type, network_name), index=False,
        float_format='%.3f')
    # node pressures
    pd.DataFrame(pressure_nodes_supply, columns=edge_node_df.index).to_csv(
        locator.get_optimization_network_layout_supply_pressure_file(network_type, network_name), index=False,
        float_format='%.3f')
    pd.DataFrame(pressure_nodes_return, columns=edge_node_df.index).to_csv(
        locator.get_optimization_network_layout_return_pressure_file(network_type, network_name), index=False,
        float_format='%.3f')
    # pressure losses over entire network
    pd.DataFrame(pressure_loss_system, columns=['pressure_loss_supply_Pa', 'pressure_loss_return_Pa',
                                                'pressure_loss_total_Pa']).to_csv(
        locator.get_optimization_network_layout_pressure_drop_file(network_type, network_name), index=False,
        float_format='%.3f')

    print("\n", time.clock() - t0, "seconds process time for thermal-hydraulic calculation of", network_type,
          " network ", network_name, "\n")


# ===========================
# Hydraulic calculation
# ===========================

def calc_mass_flow_edges(edge_node_df, mass_flow_substation_df, all_nodes_df, pipe_diameter_m, pipe_length_m,
                         T_edge_K, gv):
    """
    This function carries out the steady-state mass flow rate calculation for a predefined network with predefined mass
    flow rates at each substation based on the method from Todini et al. (1987), Ikonen et al. (2016), Oppelt et al.
    (2016), etc.

    :param all_nodes_df: DataFrame containing all nodes and whether a node n is a consumer or plant node
                        (and if so, which building that node corresponds to), or neither.
    :param edge_node_df: DataFrame consisting of n rows (number of nodes) and e columns (number of edges)
                         and indicating the direction of flow of each edge e at node n: if e points to n,
                         value is 1; if e leaves node n, -1; else, 0.                                       (n x e)
    :param mass_flow_substation_df: DataFrame containing the mass flow rate at each node n at each time
                                     of the year t
    :param pipe_diameter_m: vector containing the pipe diameter in m for each edge e in the network      (e x 1)
    :param pipe_length_m: vector containing the length in m of each edge e in the network                (e x 1)
    :param T_edge_K: matrix containing the temperature of the water in each edge e at time t             (t x e)
    :param gv: an instance of globalvar.GlobalVariables with the constants  to use (like `list_uses` etc.)

    :type all_nodes_df: DataFrame(t x n)
    :type edge_node_df: DataFrame
    :type mass_flow_substation_df: DataFrame
    :type pipe_diameter_m: ndarray
    :type pipe_length_m: ndarray
    :type T_edge_K: ndarray
    :type gv: GlobalVariables

    :return mass_flow_edge: matrix specifying the mass flow rate at each edge e at the given time step t
    :rtype mass_flow_edge: numpy.ndarray

    .. [Todini & Pilati, 1987] Todini & Pilati. "A gradient method for the analysis of pipe networks," in Computer
       Applications in Water Supply Volume 1 - Systems Analysis and Simulation, 1987.

    .. [Ikonen, E., et al, 2016] Ikonen, E., et al. Examination of Operational Optimization at Kemi District Heating
       Network. Thermal Science. 2016, Vol. 20, No.2, pp.667-678.

    .. [Oppelt, T., et al., 2016] Oppelt, T., et al. Dynamic thermo-hydraulic model of district cooling networks.
       Applied Thermal Engineering, 2016.
    """

    loops, graph = find_loops(edge_node_df)  # identifies all linear independent loops
    if loops:
        # print('Fundamental loops in the network:', loops) #returns nodes that define loop, useful for visiual verification in testing phase,

        sum_delta_m_num = np.zeros((1, len(loops)))[0]
        sum_delta_m_den = np.zeros((1, len(loops)))[0]

        # if loops exist:
        # 1. calculate initial guess solution of matrix A
        # delete first plant on an edge of matrix and solution space b as these are redundant
        plant_index = np.where(all_nodes_df['Type'] == 'PLANT')[0][0]  # find index of the first plant node
        A = edge_node_df.drop(edge_node_df.index[plant_index]) #solution matrix A without loop equations (kirchhoff 2)
        b_init = np.nan_to_num(mass_flow_substation_df.T)
        b_init = np.delete(b_init, plant_index)
        #solution vector b of node demands
        mass_flow_edge = np.linalg.lstsq(A, b_init)[0].transpose()  # solve system

        # setup iterations for implicit matrix solver
        tolerance = 0.01 # tolerance for mass flow convergence
        m_old = mass_flow_edge - mass_flow_edge #initialize m_old vector

        # begin iterations
        iterations = 0
        while (abs(mass_flow_edge - m_old) > tolerance).any(): #while difference of mass flow on any  edge > tolerance
            m_old = np.array(mass_flow_edge)  # iterate over massflow

            # calculate value similar to Hardy Cross correction factor
            # uses Hardy Cross method but a different variation for calculating the mass flow
            delta_m_num = calc_pressure_loss_pipe(pipe_diameter_m, pipe_length_m, m_old, T_edge_K,
                                                  gv, 2) * np.sign(m_old) #calculate pressure losses
            delta_m_den = abs(calc_pressure_loss_pipe(pipe_diameter_m, pipe_length_m, m_old, T_edge_K,
                                                      gv, 1)) #calculate derivatives of pressure losses
            delta_m_num = delta_m_num.transpose()

            sum_delta_m_num = np.zeros((1,len(loops)))[0]
            sum_delta_m_den = np.zeros((1,len(loops)))[0]

            for i in range(len(loops)):
                # calculate the mass flow correction for each loop
                # iterate over loops
                # save the edge number connecting the nodes of the loops into the variable index
                for j in range(len(loops[i])):
                    if j == len(loops[i]) - 1:
                        value = loops[i][0]
                    else:
                        value = loops[i][j + 1]
                    index = graph.get_edge_data(loops[i][j], value)
                    # check if nodes  defined in clockwise loop, to keep sign convention for Hardy Cross Method
                    if not (edge_node_df.iloc[loops[i][j]][index['edge_number']] == 1) & \
                           (edge_node_df.iloc[value][index['edge_number']] == -1):
                        clockwise = -1
                    else:
                        clockwise = 1
                    sum_delta_m_num[i] = sum_delta_m_num[i] + delta_m_num[index["edge_number"]] * clockwise
                    sum_delta_m_den[i] = sum_delta_m_den[i] + delta_m_den[index["edge_number"]]
                #calculate flow correction for each loop
                delta_m = -sum_delta_m_num[i] / sum_delta_m_den[i]

                # apply mass flow correction to all edges of each loop
                for j in range(len(loops[i])):
                    if j == len(loops[i]) - 1:
                        value = loops[i][0]
                    else:
                        value = loops[i][j + 1]
                    index = graph.get_edge_data(loops[i][j], value)
                    # check if nodes  defined in clockwise loop
                    if not (edge_node_df.iloc[loops[i][j]][index['edge_number']] == 1) & \
                           (edge_node_df.iloc[value][index['edge_number']] == -1):
                        clockwise = -1
                    else:
                        clockwise = 1
                    # apply loop correction
                    mass_flow_edge[index["edge_number"]] = mass_flow_edge[index["edge_number"]] + delta_m * clockwise
            iterations = iterations + 1

            # adapt tolerance to reduce total amount of iterations
            if iterations < 20:
                tolerance = 0.01
            elif iterations < 50:
                tolerance = 0.02
            elif iterations < 100:
                tolerance = 0.03
            else:
                print('No convergence of looped massflows after ', iterations, ' iterations with a remaining '
                                                                               'difference of',
                      max(abs(mass_flow_edge - m_old)), '.')
                break
        # print('Looped massflows converged after ', iterations, ' iterations.')

    else:  # no loops
        ## remove one equation (at plant node) to build a well-determined matrix, A.
        plant_index = np.where(all_nodes_df['Type'] == 'PLANT')[0][0]  # find index of the first plant node
        A = edge_node_df.drop(edge_node_df.index[plant_index])
        b = np.nan_to_num(mass_flow_substation_df.T)
        b = np.delete(b, plant_index)
        mass_flow_edge = np.linalg.solve(A.values, b)

    # verify calculated solution
    plant_index = np.where(all_nodes_df['Type'] == 'PLANT')[0][0]  # find index of the first plant node
    A = edge_node_df.drop(edge_node_df.index[plant_index])
    b_verification = A.dot(mass_flow_edge)
    b_original = np.nan_to_num(mass_flow_substation_df.T)
    b_original = np.delete(b_original, plant_index)
    if max(abs(b_original - b_verification)) > 0.01:
        print('Error in the defined mass flows, deviation of ', max(abs(b_original - b_verification)),
              ' from node demands.')
    if loops:

        if (abs(sum_delta_m_num)> 5000).any() : # 5 kPa is sufficiently small
            print('Error in the defined mass flows, deviation of ', max(abs(sum_delta_m_num)),
                  ' from 0 pressure in loop.')

    mass_flow_edge = np.round(mass_flow_edge, decimals=5)
    return mass_flow_edge


def find_loops(edge_node_df):
    """
    This function converts the input matrix into a networkx type graph and identifies all fundamental loops
    of the network. The group of fundamental loops is defined as the series of linear independent loops which
    can be combined to form all other loops.

    :param edge_node_df: DataFrame consisting of n rows (number of nodes) and e columns (number of edges)
                         and indicating the direction of flow of each edge e at node n: if e points to n,
                         value is 1; if e leaves node n, -1; else, 0.                             (n x e)

    :type edge_node_df: DataFrame

    :return: loops: list of all fundamental loops in the network
    :return: graph: networkx dictionary type graph of network

    :rtype: loops: list
    :rtype: graph: dictionary
    """
    edge_node_df_t = np.transpose(edge_node_df)  # transpose matrix to more intuitively setup graph

    graph = nx.Graph() #set up networkx type graph

    for i in range(edge_node_df_t.shape[0]):
        new_edge = [0, 0]
        for j in range(0, edge_node_df_t.shape[1]):
            if edge_node_df_t.iloc[i][edge_node_df_t.columns[j]] == 1:
                new_edge[0] = j
            elif edge_node_df_t.iloc[i][edge_node_df_t.columns[j]] == -1:
                new_edge[1] = j
        graph.add_edge(new_edge[0], new_edge[1], edge_number=i) # add edges to graph
        # edge number necessary to later identify which edges are in loop since graph is a dictionary

    loops = nx.cycle_basis(graph, 0)  # identifies all linear independent loops

    return loops, graph


def assign_pipes_to_edges(mass_flow_df, locator, gv, set_diameter, edge_df, network_type, network_name):
    """
    This function assigns pipes from the catalog to the network for a network with unspecified pipe properties.
    Pipes are assigned based on each edge's minimum and maximum required flow rate. Assuming max velocity for pipe
    DN450-550 is 3 m/s; for DN600 is 3.5 m/s. min velocity for all pipes are 0.3 m/s.

    :param mass_flow_df: DataFrame containing the mass flow rate for each edge e at each time of the year t
    :param locator: an InputLocator instance set to the scenario to work on
    :param gv: an instance of globalvar.GlobalVariables with the constants  to use (like `list_uses` etc.)
    :type mass_flow_df: DataFrame
    :type locator: InputLocator
    :type gv: GlobalVariables

    :return pipe_properties_df: DataFrame containing the pipe properties for each edge in the network


    """

    # import pipe catalog from Excel file
    pipe_catalog = pd.read_excel(locator.get_thermal_networks(), sheetname=['PIPING CATALOG'])['PIPING CATALOG']
    pipe_catalog['mdot_min_kgs'] = pipe_catalog['Vdot_min_m3s'] * gv.rho_60
    pipe_catalog['mdot_max_kgs'] = pipe_catalog['Vdot_max_m3s'] * gv.rho_60
    pipe_properties_df = pd.DataFrame(data=None, index=pipe_catalog.columns.values, columns=mass_flow_df.columns.values)
    if set_diameter:
        for pipe in mass_flow_df:
            pipe_found = False
            i = 0
            while pipe_found == False:
                if np.amax(np.absolute(mass_flow_df[pipe].values)) <= pipe_catalog['mdot_max_kgs'][i]:
                    pipe_properties_df[pipe] = np.transpose(pipe_catalog[:][i:i + 1].values)
                    pipe_found = True
                elif i == (len(pipe_catalog) - 1):
                    pipe_properties_df[pipe] = np.transpose(pipe_catalog[:][i:i + 1].values)
                    pipe_found = True
                    print(pipe, 'with maximum flow rate of', mass_flow_df[pipe].values, '[kg/s] '
                                                                                        'requires a bigger pipe than provided in the database.' '\n' 'Please add a pipe with adequate pipe '
                                                                                        'size to the Piping Catalog under ..cea/database/system/thermal_networks.xls' '\n')
                else:
                    i += 1
        # at the end save back the edges dataframe in the shapefile with the new pipe diameters
        if os.path.exists(locator.get_network_layout_edges_shapefile(network_type, network_name)):
            network_edges = gpd.read_file(locator.get_network_layout_edges_shapefile(network_type, network_name))
            network_edges['Pipe_DN'] = pipe_properties_df.loc['Pipe_DN'].values
            network_edges.to_file(locator.get_network_layout_edges_shapefile(network_type, network_name))
    else:
        for pipe, row in edge_df.iterrows():
            index = pipe_catalog.Pipe_DN[pipe_catalog.Pipe_DN == row['Pipe_DN']].index
            if len(index) == 0:  # there is no match in the pipe catalog
                raise ValueError(
                    'A very specific bad thing happened!: One or more of the pipes diameters you indicated' '\n'
                    'are not in the pipe catalog!, please make sure your input network match the piping catalog,' '\n'
                    'otherwise :P')
            pipe_properties_df[pipe] = np.transpose(pipe_catalog.loc[index].values)

    return pipe_properties_df


def calc_pressure_nodes(edge_node_df, pipe_diameter, pipe_length, edge_mass_flow, T_supply_node_k,
                        T_return_node_k, gv):
    """
    Calculates the pressure at each node based on Eq. 1 in Todini & Pilati (1987). For the pressure drop through a pipe,
    the Darcy-Weisbach equation was used as in Oppelt et al. (2016) instead of the Hazen-Williams method used by Todini
    & Pilati. Since the pressure is calculated after the mass flow rate (rather than concurrently) this is only a first
    step towards implementing the Gradient Method from Todini & Pilati used by EPANET et al.

    :param edge_node_df: DataFrame consisting of n rows (number of nodes) and e columns (number of edges) and
            indicating the direction of flow of each edge e at node n: if e points to n, value is 1; if e leaves
            node n, -1; else, 0.                                                                        (n x e)
    :param pipe_diameter: vector containing the pipe diameter in m for each edge e in the network      (e x 1)
    :param pipe_length: vector containing the length in m of each edge e in the network                (e x 1)
    :param edge_mass_flow: matrix containing the mass flow rate in each edge e at time t               (1 x e)
    :param T_supply_node_k: array containing the temperature in each supply node n                       (1 x n)
    :param T_return_node_k: array containing the temperature in each return node n                       (1 x n)
    :param gv: globalvars
    :type edge_node_df: DataFrame
    :type pipe_diameter: ndarray
    :type pipe_length: ndarray
    :type edge_mass_flow: ndarray
    :type T_supply_node_k: list
    :type T_return_node_k: list

    :return pressure_loss_nodes_supply: array containing the pressure loss at each supply node         (1 x n)
    :return pressure_loss_nodes_return: array containing the pressure loss at each return node         (1 x n)
    :return pressure_loss_system: pressure loss over the entire network
    :rtype pressure_loss_nodes_supply: ndarray
    :rtype pressure_loss_nodes_return: ndarray
    :rtype pressure_loss_system: float

    .. [Todini & Pilati, 1987] Todini & Pilati. "A gradient method for the analysis of pipe networks," in Computer
       Applications in Water Supply Volume 1 - Systems Analysis and Simulation, 1987.

    .. [Oppelt, T., et al., 2016] Oppelt, T., et al. Dynamic thermo-hydraulic model of district cooling networks.
       Applied Thermal Engineering, 2016.
    """
    ## change pipe flow directions in the edge_node_df_t according to the flow conditions
    #change_to_edge_node_matrix_t(edge_mass_flow, edge_node_df)

    # get the temperatures at each supply and return edge
    temperature_supply_edges__k = calc_edge_temperatures(T_supply_node_k, edge_node_df)
    temperature_return_edges__k = calc_edge_temperatures(T_return_node_k, edge_node_df)

    # get the pressure drop through each edge
    pressure_loss_pipe_supply__pa = calc_pressure_loss_pipe(pipe_diameter, pipe_length, edge_mass_flow,
                                                           temperature_supply_edges__k, gv, 2)
    pressure_loss_pipe_return__pa = calc_pressure_loss_pipe(pipe_diameter, pipe_length, edge_mass_flow,
                                                           temperature_return_edges__k, gv, 2)

    # total pressure loss in the system
    # # pressure losses at the supply plant are assumed to be included in the pipe losses as done by Oppelt et al., 2016
    # pressure_loss_system = sum(np.nan_to_num(pressure_loss_pipe_supply)[0]) + sum(
    #     np.nan_to_num(pressure_loss_pipe_return)[0])
    pressure_loss_system__pa = calc_pressure_loss_system(pressure_loss_pipe_supply__pa, pressure_loss_pipe_return__pa)

    # solve for the pressure at each node based on Eq. 1 in Todini & Pilati for no = 0 (no nodes with fixed head):
    # A12 * H + F(Q) = -A10 * H0 = 0
    # edge_node_transpose * pressure_nodes = - (pressure_loss_pipe) (Ax = b)
    edge_node_transpose = np.transpose(edge_node_df.values)
    pressure_nodes_supply__pa = np.round(
        np.transpose(np.linalg.lstsq(edge_node_transpose, np.transpose(pressure_loss_pipe_supply__pa) * (-1))[0]),
        decimals=5)
    pressure_nodes_return__pa = np.round(
        np.transpose(np.linalg.lstsq(-edge_node_transpose, np.transpose(pressure_loss_pipe_return__pa) * (-1))[0]),
        decimals=5)
    return pressure_nodes_supply__pa, pressure_nodes_return__pa, pressure_loss_system__pa


def change_to_edge_node_matrix_t(edge_mass_flow, edge_node_df, mass_flow_substations_nodes_df,
                                                       all_nodes_df,
                                                       pipe_properties_df,
                                                       edge_df,
                                                       t_edge__k, gv):
    """
    The function changes the flow directions in edge_node_df to align with flow directions at each time-step, this way
    all the mass flows are positive.
    :param edge_mass_flow:
    :param edge_node_df: edge node matrix
    :return:
    """
    edge_mass_flow = np.round(edge_mass_flow, decimals=5) # round to avoid very low near 0 mass flows
    while edge_mass_flow.min() < 0:
        for i in range(len(edge_mass_flow)):
            if edge_mass_flow[i] < 0:
                edge_mass_flow[i] = abs(edge_mass_flow[i])
                edge_node_df[edge_node_df.columns[i]] = -edge_node_df[edge_node_df.columns[i]]
        edge_mass_flow = calc_mass_flow_edges(edge_node_df, mass_flow_substations_nodes_df,
                                                       all_nodes_df,
                                                       pipe_properties_df[:]['D_int_m':'D_int_m'].values[0],
                                                       edge_df['pipe length'].values,
                                                       t_edge__k, gv)
    return edge_mass_flow, edge_node_df


def calc_pressure_loss_pipe(pipe_diameter_m, pipe_length_m, mass_flow_rate_kgs, t_edge__k, gv, loop_type):
    """
    Calculates the pressure losses throughout a pipe based on the Darcy-Weisbach equation and the Swamee-Jain
    solution for the Darcy friction factor [Oppelt et al., 2016].

    :param pipe_diameter_m: vector containing the pipe diameter in m for each edge e in the network           (e x 1)
    :param pipe_length_m: vector containing the length in m of each edge e in the network                     (e x 1)
    :param mass_flow_rate_kgs: matrix containing the mass flow rate in each edge e at time t                  (t x e)
    :param t_edge__k: matrix containing the temperature of the water in each edge e at time t                 (t x e)
    :param gv: an instance of globalvar.GlobalVariables with the constants  to use (like `list_uses` etc.)
    :param loop_type: int indicating if function is called from loop calculation or not, or is derivate is necessary
                        (0 = Loop, 1 = derivative of Loop, 2 = branch)
    :type pipe_diameter_m: ndarray
    :type pipe_length_m: ndarray
    :type mass_flow_rate_kgs: ndarray
    :type t_edge__k: list
    :type gv: GlobalVariables
    :type loop_type: binary

    :return pressure_loss_edge: pressure loss through each edge e at each time t                            (t x e)
    :rtype pressure_loss_edge: ndarray

    ..[Oppelt, T., et al., 2016] Oppelt, T., et al. Dynamic thermo-hydraulic model of district cooling networks.
    Applied Thermal Engineering, 2016.

    """
    reynolds = calc_reynolds(mass_flow_rate_kgs, gv, t_edge__k, pipe_diameter_m)

    darcy = calc_darcy(pipe_diameter_m, reynolds, gv.roughness)

    if loop_type == 1: # dp/dm parital derivative of edge pressure loss equation
        pressure_loss_edge_Pa = darcy * 16 * mass_flow_rate_kgs * pipe_length_m / (
                math.pi ** 2 * pipe_diameter_m ** 5 * gv.rho_60)
    else:
        # calculate the pressure losses through a pipe using the Darcy-Weisbach equation
        pressure_loss_edge_Pa = darcy * 8 * mass_flow_rate_kgs ** 2 * pipe_length_m / (
                math.pi ** 2 * pipe_diameter_m ** 5 * gv.rho_60)
    # todo: add pressure loss in valves, corners, etc., e.g. equivalent length method, or K Method
    return pressure_loss_edge_Pa


def calc_pressure_loss_system(pressure_loss_pipe_supply, pressure_loss_pipe_return):
    pressure_loss_system = np.full(3, np.nan)
    pressure_loss_system[0] = sum(np.nan_to_num(pressure_loss_pipe_supply)[0])
    pressure_loss_system[1] = sum(np.nan_to_num(pressure_loss_pipe_return)[0])
    pressure_loss_system[2] = pressure_loss_system[0] + pressure_loss_system[1]
    return pressure_loss_system


def calc_darcy(pipe_diameter_m, reynolds, pipe_roughness_m):
    """
    Calculates the Darcy friction factor [Oppelt et al., 2016].

    :param pipe_diameter_m: vector containing the pipe diameter in m for each edge e in the network           (e x 1)
    :param reynolds: vector containing the reynolds number of flows in each edge in that timestep	      (e x 1)
    :param pipe roughness_m: float with pipe roughness
    :type pipe_diameter_m: ndarray
    :type reynolds: ndarray
    :type pipe_roughness_m: float

    :return nusselt: calculated darcy friction factor for flow in each edge		(ex1)
    :rtype nusselt: ndarray

    ..[Oppelt, T., et al., 2016] Oppelt, T., et al. Dynamic thermo-hydraulic model of district cooling networks.
      Applied Thermal Engineering, 2016.

    .. Incropera, F. P., DeWitt, D. P., Bergman, T. L., & Lavine, A. S. (2007). Fundamentals of Heat and Mass Transfer.
       Fundamentals of Heat and Mass Transfer. https://doi.org/10.1016/j.applthermaleng.2011.03.022
    """

    darcy = np.zeros(reynolds.size)
    # necessary to make sure pipe_diameter is 1D vector as input formats can vary
    if hasattr(pipe_diameter_m[0], '__len__'):
        pipe_diameter_m = pipe_diameter_m[0]
    for rey in range(reynolds.size):
        if reynolds[rey] <= 1:
            darcy[rey] = 0
        elif reynolds[rey] <= 2300:
            # calculate the Darcy-Weisbach friction factor for laminar flow
            darcy[rey] = 64 / reynolds[rey]
        elif reynolds[rey] <= 5000:
            # calculate the Darcy-Weisbach friction factor for transient flow (for pipe roughness of e/D=0.0002,
            # @low reynolds numbers lines for smooth pipe nearl identical in Moody Diagram) so smooth pipe approximation used
            darcy[rey] = 0.316 * reynolds[rey] ** -0.25
        else:
            # calculate the Darcy-Weisbach friction factor using the Swamee-Jain equation, applicable for Reynolds= 5000 - 10E8; pipe_roughness=10E-6 - 0.05
            darcy[rey] = 1.325 * np.log(
                pipe_roughness_m / (3.7 * pipe_diameter_m[rey]) + 5.74 / reynolds[rey] ** 0.9) ** (-2)

    return darcy


def calc_reynolds(mass_flow_rate_kgs, gv, temperature__k, pipe_diameter_m):
    """
    Calculates the reynolds number of the internal flow inside the pipes.

    :param pipe_diameter_m: vector containing the pipe diameter in m for each edge e in the network           (e x 1)
    :param mass_flow_rate_kgs: matrix containing the mass flow rate in each edge e at time t                    (t x e)
    :param temperature__k: matrix containing the temperature of the water in each edge e at time t             (t x e)
    :param gv: an instance of globalvar.GlobalVariables with the constants  to use (like `list_uses` etc.)
    :type pipe_diameter_m: ndarray
    :type mass_flow_rate_kgs: ndarray
    :type temperature__k: list
    :type gv: GlobalVariables
    """
    kinematic_viscosity_m2s = calc_kinematic_viscosity(temperature__k)  # m2/s

    reynolds = np.nan_to_num(
        4 * (abs(mass_flow_rate_kgs) / gv.rho_60) / (math.pi * kinematic_viscosity_m2s * pipe_diameter_m))
    # necessary if statement to make sure ouput is an array type, as input formats of files can vary
    if hasattr(reynolds[0], '__len__'):
        reynolds = reynolds[0]
    return reynolds


def calc_prandtl(gv, temperature__k):
    """
    Calculates the prandtl number of the internal flow inside the pipes.

    :param temperature__k: matrix containing the temperature of the water in each edge e at time t             (t x e)
    :param gv: an instance of globalvar.GlobalVariables with the constants  to use (like `list_uses` etc.)
    :type temperature__k: list
    :type gv: GlobalVariables
    """
    kinematic_viscosity_m2s = calc_kinematic_viscosity(temperature__k)  # m2/s
    thermal_conductivity = calc_thermal_conductivity(temperature__k)  # W/(m*K)

    return np.nan_to_num(kinematic_viscosity_m2s * gv.rho_60 * gv.cp/ thermal_conductivity)


def calc_kinematic_viscosity(temperature):
    """
    Calculates the kinematic viscosity of water as a function of temperature based on a simple fit from data from the
    engineering toolbox.

    :param temperature: in K
    :return: kinematic viscosity in m2/s
    """
    # check if list type, this can cause problems
    if isinstance(temperature, (list,)):
        temperature = np.array(temperature)
    return 2.652623e-8 * math.e ** (557.5447 * (temperature - 140) ** -1)


def calc_thermal_conductivity(temperature):
    """
    Calculates the thermal conductivity of water as a function of temperature based on a fit proposed in:

    :param temperature: in K
    :return: thermal conductivity in W/(m*K)

    ... Standard Reference Data for the Thermal Conductivity of Water
    Ramires, Nagasaka, et al.
    1994

    """

    return 0.6065 * (-1.48445 + 4.12292 * temperature / 298.15 - 1.63866 * (temperature / 298.15) ** 2)


def calc_max_edge_flowrate(all_nodes_df, building_names, buildings_demands, edge_node_df, gv, locator,
                           substations_hex_specs, t_target_supply, network_type, network_name, pipe_length, edge_df,
                           set_diameter):
    """
    Calculates the maximum flow rate in the network in order to assign the pipe diameter required at each edge. This is
    done by calculating the mass flow rate required at each substation to supply the calculated demand at the target
    supply temperature for each time step, finding the maximum for each node throughout the year and calculating the
    resulting necessary mass flow rate at each edge to satisfy this demand.

    :param all_nodes_df: DataFrame containing all nodes and whether a node n is a consumer or plant node
                        (and if so, which building that node corresponds to), or neither.                   (2 x n)
    :param building_names: list of building names in the scenario
    :param buildings_demands: demand of each building in the scenario
    :param edge_node_df: DataFrame consisting of n rows (number of nodes) and e columns (number of edges)
                        and indicating the direction of flow of each edge e at node n: if e points to n,
                        value is 1; if e leaves node n, -1; else, 0.                                        (n x e)
    :param gv: an instance of globalvar.GlobalVariables with the constants  to use (like `list_uses` etc.)
    :param locator: an InputLocator instance set to the scenario to work on
    :param substations_hex_specs: DataFrame with substation heat exchanger specs at each building.
    :param t_target_supply: target supply temperature at each substation
    :param network_type: a string that defines whether the network is a district heating ('DH') or cooling
                         ('DC') network
    :param pipe_length: vector containing the length of each edge in the network
    :type all_nodes_df: DataFrame
    :type gv: GlobalVariables
    :type locator: InputLocator
    :type substations_hex_specs: DataFrame
    :type network_type: str
    :type pipe_length: array

    :return edge_mass_flow_df: mass flow rate at each edge throughout the year
    :return max_edge_mass_flow_df: maximum mass flow at each edge to be used for pipe sizing
    :rtype edge_mass_flow_df: DataFrame
    :rtype max_edge_mass_flow_df: DataFrame

    """
    ## The script below is to bypass the calculation from line 457-490, if the above calculation has been done once.
    edge_mass_flow_df = pd.read_csv(locator.get_edge_mass_flow_csv_file(network_type, network_name))
    del edge_mass_flow_df['Unnamed: 0']
    max_edge_mass_flow_df = pd.DataFrame(data=[(edge_mass_flow_df.abs()).max(axis=0)], columns=edge_node_df.columns)
    pipe_properties_df = assign_pipes_to_edges(max_edge_mass_flow_df, locator, gv, set_diameter, edge_df,
                                               network_type, network_name)

    '''
    # create empty DataFrames to store results

    edge_mass_flow_df = pd.DataFrame(data=np.zeros((8760, len(edge_node_df.columns.values))),
                                     columns=edge_node_df.columns.values)

    node_mass_flow_df = pd.DataFrame(data=np.zeros((8760, len(edge_node_df.index))),
                                     columns=edge_node_df.index.values)  # input parameters for validation

    loops, graph = find_loops(edge_node_df)

    if loops:
        print('Fundamental loops in network: ', loops)
        # initial guess of pipe diameter
        diameter_guess = initial_diameter_guess(all_nodes_df, building_names, buildings_demands, edge_node_df, gv,
                                                locator, substations_hex_specs, t_target_supply, network_type,
                                                network_name, edge_df, set_diameter)
    else:
        # no iteration necessary
        # read in diameters from shp file
        network_edges = gpd.read_file(locator.get_network_layout_edges_shapefile(network_type, network_name))
        diameter_guess = network_edges['Pipe_DN']

    print('start calculating mass flows in edges...')
    iterations = 0
    #t0 = time.clock()
    converged = False
    # Iterate over diameter of pipes since m = f(delta_p), delta_p = f(diameter) and diameter = f(m)
    while converged == False:
        print('\n Diameter iteration number ', iterations)
        diameter_guess_old = diameter_guess

        t0 = time.clock()
        for t in range(8760):

            print('\n calculating mass flows in edges... time step', t)
            min_edge_flow_flag = False
            delta_cap_mass_flow = 0
            iteration = 0
            nodes = []
            cc_old_sh = pd.DataFrame()
            cc_old_dhw = pd.DataFrame()
            ch_old = pd.DataFrame()
            while min_edge_flow_flag == False:  # too low edge mass flows
                # set to the highest value in the network and assume no loss within the network
                T_substation_supply = t_target_supply.ix[t].max() + 273.15  # in [K]

                # calculate substation flow rates and return temperatures
                if network_type == 'DH' or (network_type == 'DC' and math.isnan(T_substation_supply) == False):
                    T_return_all_K, \
                    mdot_all, \
                    cc_value_sh, \
                    cc_value_dhw, \
                    ch_value = substation.substation_return_model_main(locator, gv, building_names, buildings_demands,
                                                                       substations_hex_specs, T_substation_supply, t,
                                                                       network_type, False, delta_cap_mass_flow,
                                                                       cc_old_sh, cc_old_dhw, ch_old, nodes)

                    # t_flag = True: same temperature for all nodes
                else:
                    T_return_all_K = np.full(building_names.size, T_substation_supply).T
                    mdot_all = pd.DataFrame(data=np.zeros(len(building_names)), index=building_names.values).T
                    cc_value_sh = 0
                    cc_value_dhw = 0
                    ch_value = 0

                # write consumer substation required flow rate to nodes
                required_flow_rate_df = write_substation_values_to_nodes_df(all_nodes_df, mdot_all)
                # (1 x n)

                # initial guess temperature
                T_edge_K_initial = np.array([T_substation_supply] * edge_node_df.shape[1])

                # solve mass flow rates on edges
                edge_mass_flow_df[:][t:t + 1] = [calc_mass_flow_edges(edge_node_df, required_flow_rate_df, all_nodes_df,
                                                                          diameter_guess, pipe_length,
                                                                          T_edge_K_initial, gv)]
                node_mass_flow_df[:][t:t + 1] = required_flow_rate_df.values

                iteration, \
                min_edge_flow_flag, \
                cc_old_sh, ch_old, \
                cc_old_dhw, \
                delta_cap_mass_flow, nodes = edge_mass_flow_iteration(locator, network_type, network_name,
                                                                      edge_mass_flow_df[:][t:t + 1], iteration,
                                                                      cc_value_sh, ch_value, cc_value_dhw, edge_node_df,
                                                                      building_names, gv)

        edge_mass_flow_df.to_csv(locator.get_edge_mass_flow_csv_file(network_type, network_name))
        node_mass_flow_df.to_csv(locator.get_node_mass_flow_csv_file(network_type, network_name))

        print(time.clock() - t0, "seconds process time for edge mass flow calculation\n")

        # print(time.clock() - t0, "seconds process time and ", iterations, " iterations for diameter calculation\n")

        # assign pipe properties based on max flow on edges
        max_edge_mass_flow_df = pd.DataFrame(data=[(edge_mass_flow_df.abs()).max(axis=0)], columns=edge_node_df.columns)

        # assign pipe id/od according to maximum edge mass flow
        pipe_properties_df = assign_pipes_to_edges(max_edge_mass_flow_df, locator, gv, set_diameter, edge_df,
                                                   network_type, network_name)

        diameter_guess = pipe_properties_df[:]['D_int_m':'D_int_m'].values[0]

        #exit condition for while statement
        if (abs(diameter_guess_old - diameter_guess) > 0.005).any():
            # 0.005 is the smallest diameter change of the catalogue, so at least on diameter value has changed
            converged = False
        else: # no change of diameters
            converged = True
        if not loops: # no loops, so no iteration necessary
            converged = True
        iterations += 1
    '''
    max_edge_mass_flow_df = np.round(max_edge_mass_flow_df, decimals=5)
    return edge_mass_flow_df, max_edge_mass_flow_df, pipe_properties_df


def initial_diameter_guess(all_nodes_df, building_names, buildings_demands, edge_node_df, gv, locator,
                           substations_hex_specs, t_target_supply, network_type, network_name, edge_df, set_diameter):
    """
    This function calculates an initial guess for the pipe diameter in looped networks based on the time steps with the
    50 highest demands of the year. These pipe diameters are iterated until they converge, and this result is passed as
    an initial guess for the iteration over all time steps in an attempt to reduce total runtime.

    :param all_nodes_df: DataFrame containing all nodes and whether a node n is a consumer or plant node
                        (and if so, which building that node corresponds to), or neither.                   (2 x n)
    :param building_names: list of building names in the scenario
    :param buildings_demands: demand of each building in the scenario
    :param edge_node_df: DataFrame consisting of n rows (number of nodes) and e columns (number of edges)
                        and indicating the direction of flow of each edge e at node n: if e points to n,
                        value is 1; if e leaves node n, -1; else, 0.
    :param gv: an instance of globalvar.GlobalVariables with the constants  to use (like `list_uses` etc.)
    :param locator: an InputLocator instance set to the scenario to work on
    :param substations_hex_specs: DataFrame with substation heat exchanger specs at each building.
    :param t_target_supply: target supply temperature at each substation
    :param network_type: a string that defines whether the network is a district heating ('DH') or cooling
                         ('DC') network
    :param network_name: string with name of network
    :param edge_df: list of edges and their corresponding lengths and start and end nodes
    :param set_diameter: boolean if diameter needs to be set
    :type all_nodes_df: DataFrame
    :type building_names: list
    :type buildings_demands: list
    :type edge_node_df: DataFrame
    :type gv: GlobalVariables
    :type locator: InputLocator
    :type substations_hex_specs: DataFrame
    :type t_target_supply: list
    :type network_type: str
    :type network_name: str
    :type edge_df: DataFrame
    :type set_diameter: bool

    :return pipe_properties_df[:]['D_int_m':'D_int_m'].values: initial guess pipe diameters for all edges
    :rtype pipe_properties_df[:]['D_int_m':'D_int_m'].values: array
    """

    # Identify time steps of highest 50 demands
    if network_type == 'DH':
        heating_sum = buildings_demands[0].Qhsf_kWh.values + buildings_demands[0].Qwwf_kWh.values
        for i in range(1, len(buildings_demands)):
            # sum up heat demands of all buildings for dhw and sh to create (1xt) array
            heating_sum = heating_sum + buildings_demands[i].Qhsf_kWh.values + buildings_demands[i].Qwwf_kWh.values
        timesteps_top_demand = np.argsort(heating_sum)[-50:]  # identifies 50 time steps with largest demand
    else:
        cooling_sum = abs(buildings_demands[0].Qcsf_kWh.values)
        for i in range(1, len(buildings_demands)):  # sum up cooling demands of all buildings to create (1xt) array
            cooling_sum = cooling_sum + abs(buildings_demands[i].Qcsf_kWh.values)
        timesteps_top_demand = np.argsort(cooling_sum)[-50:]  # identifies 50 time steps with largest demand

    # initialize reduced copy of target temperatures
    t_target_supply_reduced = pd.DataFrame(t_target_supply)
    # Cut out relevant parts of data matching top 50 time steps
    t_target_supply_reduced = t_target_supply_reduced.iloc[timesteps_top_demand].sort_index()
    # re-index dataframe
    t_target_supply_reduced = t_target_supply_reduced.reset_index(drop=True)

    # initialize reduced copy of building demands
    buildings_demands_reduced = list(buildings_demands)
    # Cut out relevant parts of data matching top 50 time steps
    for i in range(len(buildings_demands_reduced)):
        buildings_demands_reduced[i] = buildings_demands_reduced[i].iloc[timesteps_top_demand].sort_index()
        buildings_demands_reduced[i] = buildings_demands_reduced[i].reset_index(drop=True)

    # initialize mass flows to calculate maximum edge mass flow
    edge_mass_flow_df = pd.DataFrame(data=np.zeros((50, len(edge_node_df.columns.values))),
                                     columns=edge_node_df.columns.values)

    node_mass_flow_df = pd.DataFrame(data=np.zeros((50, len(edge_node_df.index))),
                                     columns=edge_node_df.index.values)  # input parameters for validation

    print('start calculating mass flows in edges for initial guess...')
    # initial guess of pipe diameter and edge temperatures
    diameter_guess = np.array(
        [0.2] * edge_node_df.shape[1])
    # large enough for most applications
    # larger causes more iterations, smaller can cause high pressure losses in some edges

    # initialize diameter guess
    diameter_guess_old = np.array([0] * edge_node_df.shape[1])

    iterations = 0
    #t0 = time.clock()
    while (abs(
            diameter_guess_old - diameter_guess) > 0.005).any():
        # 0.005 is the smallest diameter change of the catalogue
        print('\n Initial Diameter iteration number ', iterations)
        diameter_guess_old = diameter_guess
        delta_cap_mass_flow = 0
        nodes = []
        cc_old_sh = pd.DataFrame()
        cc_old_dhw = pd.DataFrame()
        ch_old = pd.DataFrame()
        for t in range(50):
            print('\n calculating mass flows in edges... time step', t)

            # set to the highest value in the network and assume no loss within the network
            t_substation_supply = t_target_supply_reduced.iloc[t].max() + 273.15  # in [K]

            # calculate substation flow rates and return temperatures
            if network_type == 'DH' or (network_type == 'DC' and math.isnan(t_substation_supply) == False):
                T_return_all_K, \
                mdot_all_kgs, \
                cc_value_sh, \
                cc_value_dhw, \
                ch_value = substation.substation_return_model_main(locator, gv, building_names,
                                                                   buildings_demands_reduced,
                                                                   substations_hex_specs, t_substation_supply, t,
                                                                   network_type, False, delta_cap_mass_flow, cc_old_sh,
                                                                   cc_old_dhw, ch_old, nodes)


                # t_flag = True: same temperature for all nodes
            else:
                T_return_all_K = np.full(building_names.size, t_substation_supply).T
                mdot_all_kgs = pd.DataFrame(data=np.zeros(len(building_names)), index=building_names.values).T

            # write consumer substation required flow rate to nodes
            required_flow_rate_df = write_substation_values_to_nodes_df(all_nodes_df, mdot_all_kgs)
            # (1 x n)

            # initialize edge temperatures
            T_edge_initial_K = np.array([t_substation_supply] * edge_node_df.shape[1])

            if required_flow_rate_df.abs().max(axis=1)[0] != 0:  # non 0 demand
                # solve mass flow rates on edges
                edge_mass_flow_df[:][t:t + 1] = [calc_mass_flow_edges(edge_node_df, required_flow_rate_df, all_nodes_df,
                                                                      diameter_guess, edge_df['pipe length'].values,
                                                                      T_edge_initial_K, gv)]
            node_mass_flow_df[:][t:t + 1] = required_flow_rate_df.values

        # assign pipe properties based on max flow on edges
        max_edge_mass_flow_df = pd.DataFrame(data=[(edge_mass_flow_df.abs()).max(axis=0)], columns=edge_node_df.columns)

        # assign pipe id/od according to maximum edge mass flow
        pipe_properties_df = assign_pipes_to_edges(max_edge_mass_flow_df, locator, gv, set_diameter, edge_df,
                                                   network_type, network_name)
        # update diameter guess
        diameter_guess = pipe_properties_df[:]['D_int_m':'D_int_m'].values[0]
        iterations += 1

    # print(time.clock() - t0, "seconds process time and ", iterations, " iterations for initial guess edge mass flow calculation\n")
    # return converged diameter based on top 50 demand time steps
    return pipe_properties_df[:]['D_int_m':'D_int_m'].values[0]


def read_max_edge_flowrate(edge_node_df, locator, network_type):
    """
    This is a temporary function to read from file and save run time for 'calc_max_edge_flowrate'.

    :param edge_node_df: DataFrame consisting of n rows (number of nodes) and e columns (number of edges)
                        and indicating the direction of flow of each edge e at node n: if e points to n,
                        value is 1; if e leaves node n, -1; else, 0.                                        (n x e)
    :param locator: an InputLocator instance set to the scenario to work on
    :param network_type: a string that defines whether the network is a district heating ('DH') or cooling
                        ('DC') network
    :type edge_node_df: DataFrame
    :type locator: InputLocator
    :type network_type: str

    :return edge_mass_flow_df: mass flow rate at each edge throughout the year
    :return max_edge_mass_flow_df: maximum mass flow at each edge to be used for pipe sizing
    :rtype edge_mass_flow_df: DataFrame
    :rtype max_edge_mass_flow_df: DataFrame
    """

    edge_mass_flow_df = pd.read_csv(locator.get_optimization_network_layout_folder() + '//' + 'NominalEdgeMassFlow_' +
                                    network_type + '.csv')
    del edge_mass_flow_df['Unnamed: 0']

    # find maximum mass flow rate on each edges in order to assign pipe properties
    max_edge_mass_flow = edge_mass_flow_df.max(axis=0)
    max_edge_mass_flow_df = pd.DataFrame(data=[max_edge_mass_flow], columns=edge_node_df.columns)

    return edge_mass_flow_df, max_edge_mass_flow_df


def calc_edge_temperatures(temperature_node, edge_node):
    """
    Calculates the temperature at each edge assuming the average temperature in the edge is equal to the average of the
    temperatures at its start and end node as done, for example, by Wang et al. (2016), that is::

        T_edge = (T_node_1 + T_node_2)/2

    :param temperature_node: array containing the temperature in each node n                                (1 x n)
    :param edge_node: matrix consisting of n rows (number of nodes) and e columns (number of edges) and
                      indicating the direction of flow of each edge e at node n: if e points to n, value
                      is 1; if e leaves node n, -1; else, 0.                                                (n x e)

    :return temperature_edge: array containing the temperature in each edge e                               (1 x n)

    ..[Wang et al., 2016] Wang et al. "A method for the steady-state thermal simulation of district heating systems and
    model parameters calibration," in Energy Conversion and Management Vol. 120, 2016, pp. 294-305.
    """

    # necessary to avoid nan propagation in edge temperature vector.
    # E.g. if node 1 = 300 K, node 2 = nan: T_edge = 150K -> nan.
    # solution is to replace nan with the mean temperature of all nodes
    temperature_node_mean = np.nanmean(temperature_node)
    temperature_node[np.isnan(temperature_node)] = temperature_node_mean

    # in order to calculate the edge temperatures, node temperature values of 'nan' were not acceptable
    # so these were converted to 0 and then converted back to 'nan'
    temperature_edge = np.dot(np.nan_to_num(temperature_node), abs(edge_node) / 2)
    temperature_edge[temperature_edge < 273.15] = np.nan
    # todo: could be updated with more accurate exponential temperature profile of edges for mean pipe temperature,
    # or mean value of that function to avoid spacial component
    return temperature_edge


# ===========================
# Thermal calculation
# ===========================


def solve_network_temperatures(locator, gv, T_ground, edge_node_df, all_nodes_df, edge_mass_flow_df,
                               T_target_supply_df, building_names, buildings_demands, substations_HEX_specs, t,
                               network_type, network_name, edge_df, pipe_properties_df):

    """
    This function calculates the node temperatures at time-step t accounting for heat losses throughout the network.
    There is one iteration to determine weather the substation supply temperature and the substation mass flow are
    cohesive. It is done as follow: The substation supply temperatures (T_substation_supply) are calculated based on the
    nominal edge flow rate (see `calc_max_edge_flowrate`), and then the substation mass flow requirements
    (mass_flow_substation_nodes_df) and pipe mass flows (edge_mass_flow_df_2) are updated accordingly. Following, the
    substation supply temperatures(T_substation_supply_2) are recalcuated with the updated pipe mass flow.

    The iteration continues until the substation supply temperatures converged.

    Lastly, the plant heat requirements are calculated base on the plant supply/return temperatures and flow rates.

    :param locator: an InputLocator instance set to the scenario to work on
    :param gv: an instance of globalvar.GlobalVariables with the constants  to use (like `list_uses` etc.)
    :param t_ground: vector with ground temperatures in K
    :param edge_node_df: DataFrame consisting of n rows (number of nodes) and e columns (number of edges)
                        and indicating the direction of flow of each edge e at node n: if e points to n,
                        value is 1; if e leaves node n, -1; else, 0.                                        (n x e)
    :param all_nodes_df: DataFrame containing all nodes and whether a node n is a consumer or plant node
                        (and if so, which building that node corresponds to), or neither.                   (2 x n)
    :param edge_mass_flow_df: mass flow rate at each edge throughout the year
    :param t_target_supply_df: target supply temperature at each substation
    :param building_names: list of building names in the scenario
    :param buildings_demands: demand of each building in the scenario
    :param substations_hex_specs: DataFrame with substation heat exchanger specs at each building.
    :param t: current time step
    :param network_type: a string that defines whether the network is a district heating ('DH') or cooling
                        ('DC') network
    :param edge_df: list of edges and their corresponding lengths and start and end nodes
    :param pipe_properties_df: DataFrame containing the pipe properties for each edge in the network

    :type locator: InputLocator
    :type gv: GlobalVariables
    :type edge_node_df: DataFrame
    :type all_nodes_df: DataFrame
    :type edge_mass_flow_df: DataFrame
    :type locator: InputLocator
    :type substations_hex_specs: DataFrame
    :type network_type: str
    :type t_target_supply_df: DataFrame
    :type edge_df: DataFrame
    :type pipe_properties_df: DataFrame

    :returns T_supply_nodes: list of supply line node temperatures (nx1)
    :rtype T_supply_nodes: list of arrays
    :returns T_return_nodes: list of return line node temperatures (nx1)
    :rtype T_return_nodes: list of arrays
    :returns plant_heat_requirement: list of plant heat requirement
    :rtype plant_heat_requirement: list of arrays

    """

    if np.absolute(edge_mass_flow_df.values).sum() != 0:
        # initialize target temperatures in Kelvin as initial value for K_value calculation
        initial_guess_temp = np.asarray(T_target_supply_df.loc[t] + 273.15, order='C')
        t_edge__k = calc_edge_temperatures(initial_guess_temp, edge_node_df)

        # initialization of K_value
        k = calc_aggregated_heat_conduction_coefficient(edge_mass_flow_df, locator, gv, edge_df,
                                                        pipe_properties_df, t_edge__k, network_type)  # [kW/K]

        ## calculate node temperatures on the supply network accounting losses in the network.
        T_supply_nodes_K, plant_node, q_loss_edges_kw = calc_supply_temperatures(gv, T_ground[t], edge_node_df,
                                                                                  edge_mass_flow_df, k,
                                                                                  T_target_supply_df.loc[t],
                                                                                  network_type, all_nodes_df)

        # write supply temperatures to substation nodes
        T_substations_supply_K = write_nodes_values_to_substations(T_supply_nodes_K, all_nodes_df)

        ## iterations to find out the corresponding node supply temperature and substation mass flow
        flag = 0
        iteration = 0
        while flag == 0:
            # calculate substation return temperatures according to supply temperatures
            consumer_building_names = all_nodes_df.loc[all_nodes_df['Type'] == 'CONSUMER', 'Building'].values

            min_edge_flow_flag = False
            delta_cap_mass_flow = 0
            min_iteration = 0
            cc_old_sh = pd.DataFrame()
            cc_old_dhw = pd.DataFrame()
            ch_old = pd.DataFrame()
            nodes = []
            while min_edge_flow_flag == False:
                T_return_all_K, \
                mdot_all_kgs, \
                cc_value_sh, \
                cc_value_dhw, \
                ch_value = substation.substation_return_model_main(locator, gv, consumer_building_names,
                                                                    buildings_demands,
                                                                    substations_HEX_specs, T_substations_supply_K, t,
                                                                    network_type, False, delta_cap_mass_flow, cc_old_sh,
                                                                    cc_old_dhw, ch_old, nodes)
                if mdot_all_kgs.values.max() == np.nan:
                    print('Error in edge mass flow! Check edge_mass_flow_df')

                # write consumer substation return T and required flow rate to nodes
                # T_substation_return_df = write_substation_temperatures_to_nodes_df(all_nodes_df, T_return_all_K)  # (1 x n) #todo:potentially redundant
                mass_flow_substations_nodes_df = write_substation_values_to_nodes_df(all_nodes_df, mdot_all_kgs)

                # solve for the required mass flow rate on each pipe
                edge_mass_flow_df_2_kgs = calc_mass_flow_edges(edge_node_df, mass_flow_substations_nodes_df,
                                                               all_nodes_df,
                                                               pipe_properties_df[:]['D_int_m':'D_int_m'].values[0],
                                                               edge_df['pipe length'].values,
                                                               t_edge__k, gv)

                min_iteration, \
                min_edge_flow_flag, \
                cc_old_sh, ch_old, \
                cc_old_dhw, \
                delta_cap_mass_flow, \
                nodes = edge_mass_flow_iteration(locator, network_type, network_name, edge_mass_flow_df_2_kgs,
                                                 min_iteration, cc_value_sh, ch_value, cc_value_dhw, edge_node_df,
                                                 building_names, gv)

            edge_node_df_2 = edge_node_df.copy()
            edge_mass_flow_df_2_kgs, edge_node_df_2 = change_to_edge_node_matrix_t(edge_mass_flow_df_2_kgs, edge_node_df_2, mass_flow_substations_nodes_df,
                                                       all_nodes_df,
                                                       pipe_properties_df,
                                                       edge_df,
                                                       t_edge__k, gv)

            # calculate updated pipe aggregated heat conduction coefficient with new mass flows
            k = calc_aggregated_heat_conduction_coefficient(edge_mass_flow_df_2_kgs, locator, gv, edge_df,
                                                            pipe_properties_df, t_edge__k, network_type)  # [kW/K]

            # calculate updated node temperatures on the supply network with updated edge mass flow
            t_supply_nodes_2__k, plant_node, q_loss_edges_2_kw = calc_supply_temperatures(gv, T_ground[t],
                                                                                         edge_node_df,
                                                                                         edge_mass_flow_df_2_kgs, k,
                                                                                         T_target_supply_df.loc[t],
                                                                                         network_type, all_nodes_df)
            # calculate edge temperature for heat transfer coefficient within iteration
            t_edge__k = calc_edge_temperatures(t_supply_nodes_2__k, edge_node_df)

            # write supply temperatures to substation nodes
            T_substation_supply_2 = write_nodes_values_to_substations(t_supply_nodes_2__k, all_nodes_df)

            # check if the supply temperature at substations converged
            node_dt = T_substation_supply_2 - T_substations_supply_K
            if node_dt.dropna(axis=1).empty == True:
                max_node_dt = 0
            else:
                max_node_dt = max(abs(node_dt).dropna(axis=1).values[0])
                # max supply node temperature difference

            if max_node_dt > 1 and iteration < 10:
                # update the substation supply temperature and re-enter the iteration
                T_substations_supply_K = T_substation_supply_2
                # print(iteration, 'iteration. Maximum node temperature difference:', max_node_dT)
                iteration += 1
            elif max_node_dt > 10 and 20 > iteration >= 10:
                # FIXME: This is to avoid endless iteration, other design strategies should be implemented.
                # update the substation supply temperature and re-enter the iteration
                T_substations_supply_K = T_substation_supply_2
                # print(iteration, 'iteration. Maximum node temperature difference:', max_node_dT)
                iteration += 1
            else:
                min_edge_flow_flag = False
                delta_cap_mass_flow = 0
                min_iteration = 0
                cc_old_sh = pd.DataFrame()
                cc_old_dhw = pd.DataFrame()
                ch_old = pd.DataFrame()
                nodes = []
                while min_edge_flow_flag == False:
                    # calculate substation return temperatures according to supply temperatures
                    T_return_all_2, \
                    mdot_all_2, \
                    cc_value_sh, \
                    cc_value_dhw, \
                    ch_value = substation.substation_return_model_main(locator, gv, building_names, buildings_demands,
                                                                       substations_HEX_specs, T_substation_supply_2, t,
                                                                       network_type, False, delta_cap_mass_flow,
                                                                       cc_old_sh, cc_old_dhw, ch_old, nodes)
                    # write consumer substation return T and required flow rate to nodes
                    T_substation_return_df_2 = write_substation_temperatures_to_nodes_df(all_nodes_df,
                                                                                         T_return_all_2)  # (1xn)
                    mass_flow_substations_nodes_df_2 = write_substation_values_to_nodes_df(all_nodes_df, mdot_all_2)
                    # solve for the required mass flow rate on each pipe, using the nominal edge node matrix
                    edge_mass_flow_df_2_kgs = calc_mass_flow_edges(edge_node_df_2, mass_flow_substations_nodes_df_2,
                                                                   all_nodes_df,
                                                                   pipe_properties_df[:]['D_int_m':'D_int_m'].values[0],
                                                                   edge_df['pipe length'].values,
                                                                   t_edge__k, gv)

                    min_iteration, \
                    min_edge_flow_flag, \
                    cc_old_sh, ch_old, \
                    cc_old_dhw, \
                    delta_cap_mass_flow, \
                        nodes= edge_mass_flow_iteration(locator, network_type, network_name, edge_mass_flow_df_2_kgs,
                                                        min_iteration, cc_value_sh, ch_value, cc_value_dhw, edge_node_df,
                                                        building_names, gv)

                # make sure that all mass flows are still positive after last calculation
                    edge_mass_flow_df_2_kgs, edge_node_df_2 = change_to_edge_node_matrix_t(edge_mass_flow_df_2_kgs,
                                                                                           edge_node_df_2,
                                                                                           mass_flow_substations_nodes_df,
                                                                                           all_nodes_df,
                                                                                           pipe_properties_df,
                                                                                           edge_df,
                                                                                           t_edge__k, gv)

                # exit iteration
                flag = 1
                if not max_node_dt < 1:
                    #print('supply temperature converged after', iteration, 'iterations.', 'dT:', max_node_dT)
                    #else:
                    print('Warning: supply temperature did not converge after', iteration, 'iterations at timestep', t,
                          '. dT:', max_node_dt)

        # calculate node temperatures on the return network
        # edge-node matrix at the current time-step
        edge_mass_flow_df_t = calc_mass_flow_edges(edge_node_df, mass_flow_substations_nodes_df_2,
                                                   all_nodes_df,
                                                   pipe_properties_df[:]['D_int_m':'D_int_m'].values[0],
                                                   edge_df['pipe length'], t_edge__k, gv)

        # calculate final edge temperature and heat transfer coefficient
        # todo: suboptimal because using supply temperatures (limited effect since effects only water conductivity). Could be solved by iteration.
        k = calc_aggregated_heat_conduction_coefficient(edge_mass_flow_df_2_kgs, locator, gv, edge_df,
                                                        pipe_properties_df, t_edge__k, network_type)  # [kW/K]

        edge_mass_flow_df_t, edge_node_df = change_to_edge_node_matrix_t(edge_mass_flow_df_t, edge_node_df,
                                                                         mass_flow_substations_nodes_df,
                                                                         all_nodes_df,
                                                                         pipe_properties_df,
                                                                         edge_df,
                                                                         t_edge__k, gv)

        t_return_nodes_2__k = calc_return_temperatures(gv, T_ground[t], edge_node_df, edge_mass_flow_df_t,
                                                      mass_flow_substations_nodes_df_2, k, T_substation_return_df_2)

        # calculate plant heat requirements according to plant supply/return temperatures
        plant_heat_requirement_kw = calc_plant_heat_requirement(plant_node, t_supply_nodes_2__k, t_return_nodes_2__k,
                                                                mass_flow_substations_nodes_df_2, gv)

    else:
        t_supply_nodes_2__k = np.full(edge_node_df.shape[0], np.nan)
        t_return_nodes_2__k = np.full(edge_node_df.shape[0], np.nan)
        q_loss_edges_2_kw = np.full(edge_node_df.shape[1], 0)
        edge_mass_flow_df_2_kgs = edge_mass_flow_df
        plant_heat_requirement_kw = np.full(sum(all_nodes_df['Type'] == 'PLANT'), 0)

    return t_supply_nodes_2__k, t_return_nodes_2__k, plant_heat_requirement_kw, edge_mass_flow_df_2_kgs, \
           q_loss_edges_2_kw


def edge_mass_flow_iteration(locator, network_type, network_name, edge_mass_flow_df, min_iteration, cc_value_sh,
                             ch_value, cc_value_dhw, edge_node_df, building_names, gv):
    """

    :param network_type: string with network type, DH or DC
    :param edge_mass_flow_df: edge mass flows                       (1 x e)
    :param min_iteration: iteration counter
    :param cc_value_sh: capacity mass flow for space heating        (1 x e)
    :param ch_value: capacity mass flow for cooling                 (1 x e)
    :param cc_value_dhw: capacity mass flow for warm water          (1 x e)

    :return:
    """

    #todo: reactivate this once merged with looped code
    ''' 
    # read in minimum mass flow lookup table
    pipe_min_mass_flow = []
    pipe_catalog = pd.read_excel(locator.get_thermal_networks(), sheetname=['PIPING CATALOG'])['PIPING CATALOG']

    for diameter in edge_diameters:
        pipe_min_mass_flow.append(pipe_catalog.loc[pipe_catalog['D_int_m'] == diameter]['Vdot_min_m3s'] * gv.rho_60)
    '''

    min_edge_flows = 0.1  # read in minimum mass flows #todo: replace this with part above
    cc_old_sh = 0
    ch_old = 0
    cc_old_dhw = 0
    delta_cap_mass_flow = 0
    nodes = []
    if isinstance(edge_mass_flow_df, pd.DataFrame):
        test_edge_flow = edge_mass_flow_df
    else:
        test_edge_flow = pd.DataFrame(edge_mass_flow_df)
    test_edge_flow = test_edge_flow.abs()
    test_edge_flow[test_edge_flow == 0] = np.nan
    if np.isnan(test_edge_flow).values.all():
        min_edge_flow_flag = True  # no mass flows
    elif (test_edge_flow - min_edge_flows < -0.01).values.any():  # some edges have too low mass flows
        if min_iteration < 5: #identify buildings connected to edges with low mass flows
            # read in all nodes file
            node_type = pd.read_csv(locator.get_network_node_types_csv_file(network_type, network_name))['Building']
            #identify which edges
            edges = np.where((test_edge_flow - min_edge_flows < -0.01).values)[1]
            if len(edges) < len(building_names)/2: #time intensive calculation. Only worth it if only isolated edges have low mass flows
                #identify which nodes, pass these on
                for i in edges:
                    pipe_name = str(edge_node_df.columns.values[i])
                    node = np.where(edge_node_df[pipe_name] == 1)[0][0]
                    # check if node is a building
                    # if not, identify closest  building
                    while not any(node_type[node] in s for s in building_names):
                        node_name = str(edge_node_df.index.values[node])
                        if len(np.where(edge_node_df.ix[node_name] == -1)[0]) > 1:  # valid if e.g. if more than one flow and all flows incoming. Only need to flip one.
                            new_edge = random.choice(np.where(edge_node_df.ix[node_name] == -1)[0])
                        else:
                            if np.where(edge_node_df.ix[node_name] == -1)[0]:
                                new_edge = np.where(edge_node_df.ix[node_name] == -1)[0][0]
                            else:
                                min_iteration = 5 #exit for loop
                                break
                        pipe_name = str(edge_node_df.columns.values[new_edge])
                        if len(np.where(edge_node_df[pipe_name] == 1)[0]) > 1:  # valid if e.g. if more than one flow and all flows incoming. Only need to flip one.
                            node = random.choice(np.where(edge_node_df[pipe_name] == 1)[0])
                        else:
                            node = np.where(edge_node_df[pipe_name] == 1)[0][0]
                    node = node_type[node]
                    nodes.append(node)
            else: #many edges with low mass flows
                nodes = building_names
        else:  # many edges with low mass flows
            nodes = building_names
        delta_cap_mass_flow = abs(
            np.nanmin(
                (test_edge_flow.abs() - min_edge_flows).values))  # deviation from minimum mass flow
        min_edge_flow_flag = False  # need to iterate
        if network_type == 'DH':
            cc_old_sh = cc_value_sh
        else:
            ch_old = ch_value
        cc_old_dhw = cc_value_dhw
        min_iteration = min_iteration + 1
    else:  # all edge mass flows ok
        min_edge_flow_flag = True

    #exit condition
    if min_iteration > 30:
        print('Stopped minimum edge mass flow iterations at: ', min_iteration,
              'iterations with remaining delta = ', delta_cap_mass_flow)
        min_edge_flow_flag = True
    nodes = np.array(nodes)
    return min_iteration, min_edge_flow_flag, cc_old_sh, ch_old, cc_old_dhw, delta_cap_mass_flow, nodes


def calc_plant_heat_requirement(plant_node, T_supply_nodes, T_return_nodes, mass_flow_substations_nodes_df, gv):
    """
    calculate plant heat requirements according to plant supply/return temperatures and flow rate
    :param plant_node: list of plant nodes
    :param t_supply_nodes: node temperatures on the supply network
    :param t_return_nodes: node temperatures on the return network
    :param mass_flow_substations_nodes_df: substation mass flows
    :param gv: global variable
    :type plant_node: ndarray
    :type t_supply_nodes: ndarray
    :type t_return_nodes: ndarray
    :type mass_flow_substations_nodes_df: pandas dataframe
    :return:
    """
    plant_heat_requirement_kw = np.full(plant_node.size, np.nan)
    for i in range(plant_node.size):
        node = plant_node[i]
        heat_requirement = gv.cp/1000 * (T_supply_nodes[node] - T_return_nodes[node]) * abs(
            mass_flow_substations_nodes_df.iloc[0, node])
        plant_heat_requirement_kw[i] = heat_requirement
    return plant_heat_requirement_kw


def write_nodes_values_to_substations(t_supply_nodes, all_nodes_df):
    """
    This function writes node values to the corresponding building substations.

    :param t_supply_nodes: DataFrame of supply line node temperatures (nx1)
    :param all_nodes_df: DataFrame that contains all nodes, whether a node is a consumer, plant, or neither,
                        and, if it is a consumer or plant, the name of the corresponding building               (2 x n)

    :type t_supply_nodes: DataFrame
    :type all_nodes_df: DataFrame

    :return T_substation_supply: dataframe with node values matched to building substations
    :rtype T_substation_supply: DataFrame
    """
    all_nodes_df['T_supply'] = t_supply_nodes
    t_substation_supply = all_nodes_df[all_nodes_df.Building != 'NONE'].set_index(['Building'])
    t_substation_supply = t_substation_supply.drop('Type', axis=1)
    return t_substation_supply.T


def calc_supply_temperatures(gv, t_ground__k, edge_node_df, mass_flow_df, k, t_target_supply__c, network_type,
                             all_nodes_df):
    """
    This function calculate the node temperatures considering heat losses in the supply network.
    Starting from the plant supply node, the function go through the edge-node index to search for the outlet node, and
    calculate the outlet node temperature after heat loss. And starting from the outlet node, the function calculates
    the node temperature at the corresponding pipe outlet, and the calculation goes on until all the node temperatures
    are solved. At nodes connecting to multiple pipes, the mixing temperature is calculated.

    :param gv: an instance of globalvar.GlobalVariables with the constants  to use (like `list_uses` etc.)
    :param t_ground__k: vector with ground temperatures in K
    :param edge_node_df: DataFrame consisting of n rows (number of nodes) and e columns (number of edges)
                        and indicating the direction of flow of each edge e at node n: if e points to n,
                        value is 1; if e leaves node n, -1; else, 0.                                        (n x e)
    :param mass_flow_df: DataFrame containing the mass flow rate for each edge e at each time of the year t (1 x e)
    :param k: aggregated heat conduction coefficient for each pipe                                          (1 x e)
    :param t_target_supply__c: target supply temperature at each substation
    :param network_type: a string that defines whether the network is a district heating ('DH') or cooling ('DC')
                         network

    :type gv: GlobalVariables
    :type edge_node_df: DataFrame
    :type mass_flow_df: DataFrame
    :type network_type: str

    :return t_node.T: list of node temperatures (nx1)
    :return plant_node: the index of the plant node
    :rtype t_node.T: list
    :rtype plant_node: numpy array

    """
    z = np.asarray(edge_node_df)  # (nxe) edge-node matrix
    z_pipe_out = z.clip(min=0)  # pipe outlet matrix
    z_pipe_in = z.clip(max=0)  # pipe inlet matrix

    m_d = np.zeros((z.shape[1], z.shape[1]))  # (exe) pipe mass flow rate matrix
    np.fill_diagonal(m_d, mass_flow_df)

    # matrices to store results
    t_e_out = z_pipe_out.copy()
    t_e_in = z_pipe_in.copy().dot(-1)
    t_node = np.zeros(z.shape[0])
    z_note = z.copy()  # matrix to store information of solved nodes

    # start node temperature calculation
    flag = 0
    # set initial supply temperature guess to the target substation supply temperature
    t_plant_sup_0 = 273.15 + t_target_supply__c.max() if network_type == 'DH' else 273.15 + t_target_supply__c.min()
    t_plant_sup = t_plant_sup_0
    iteration = 0
    while flag == 0:
        # not_stuck variable is necessary because of looped networks. Here it is possible that we have only a closed
        # loop remaining and no obvious place to start. In this case, iteration with an initial value is necessary
        not_stuck = np.array([True] * z.shape[0])
        # count number of iterations
        temp_iter = 0
        # tolerance for convergence of temperature
        temp_tolerance = 1
        # initialize delta to some value above the tolerance
        delta_temp_0 = 2
        #iterate over temperatures for loop networks
        while delta_temp_0 >= temp_tolerance:
            t_e_out_old = np.array(t_e_out)
            # reset_matrixes
            z_note = z.copy()
            t_e_out = z_pipe_out.copy()
            t_e_in = z_pipe_in.copy().dot(-1)
            t_node = np.zeros(z.shape[0])

            # # calculate the pipe outlet temperature from the plant node
            for i in range(z.shape[0]):
                if all_nodes_df.iloc[i]['Type'] == 'PLANT':  # find plant node
                    # write plant inlet temperature
                    t_node[i] = t_plant_sup  # assume plant inlet temperature
                    edge = np.where(t_e_in[i] != 0)[0]  # find edge index
                    t_e_in[i] = t_e_in[i] * t_node[i]
                    # calculate pipe outlet temperature
                    calc_t_out(i, edge, k, m_d, z, t_e_in, t_e_out, t_ground__k, z_note, gv)
            plant_node = t_node.nonzero()[0]  # the node indices of the plant nodes in the edge-node index

            # # calculate pipe outlet temperature and node temperature for the rest
            while np.count_nonzero(t_node == 0) > 0:
                if not_stuck.any():  # if there are no changes for all elements but we have not yet solved the system
                    z, z_note, m_d, t_e_out, z_pipe_out, t_node, t_e_in, t_ground__k, not_stuck = calculate_outflow_temp(
                        z,
                        z_note,
                        m_d,
                        t_e_out,
                        z_pipe_out,
                        t_node,
                        t_e_in,
                        t_ground__k,
                        not_stuck,
                        k,
                        gv)
                else:  # stuck! this can happen with loops
                    for i in range(np.shape(t_e_out)[1]):
                        if np.any(t_e_out[:, i] == 1):
                            z_note[np.where(t_e_out[:, i] == 1), i] = 0  # remove inflow value from z_note
                            if temp_iter < 1: # do this in first iteration only, since there is no previous value
                                t_e_out[np.where(t_e_out[:, i] == 1), i] = t_node[
                                    t_node.nonzero()].mean()  # assume some node temperature
                            else:
                                t_e_out[np.where(t_e_out[:, i] == 1), i] = t_e_out_old[np.where(t_e_out[:, i] == 1), i]
                            break
                    not_stuck = np.array([True] * z.shape[0])

            delta_temp_0 = np.max(abs(t_e_out_old - t_e_out))
            temp_iter = temp_iter + 1

        # # iterate the plant supply temperature until all the node temperature reaches the target temperatures
        if network_type == 'DH':
            # calculate the difference between node temperature and the target supply temperature at substations
            # [K] temperature differences b/t node supply and target supply
            d_t = (t_node - (t_target_supply__c + 273.15)).dropna()
            # enter iteration if the node supply temperature is lower than the target supply temperature
            # (0.1 is the tolerance)
            if all(d_t > -0.1) == False and (t_plant_sup - t_plant_sup_0) < 60:
                # increase plant supply temperature and re-iterate the node supply temperature calculation
                # increase by the maximum amount of temperature deficit at nodes
                t_plant_sup = t_plant_sup + abs(d_t.min())
                # check if this term is positive, looping causes t_e_out to sink instead of rise.

                # reset the matrices for supply network temperature calculation
                z_note = z.copy()
                t_e_out = z_pipe_out.copy()
                t_e_in = z_pipe_in.copy().dot(-1)
                t_node = np.zeros(z.shape[0])
                iteration += 1

            elif all(d_t > -0.1) == False and (t_plant_sup - t_plant_sup_0) >= 60:
                # TODO: implement minimum mass flow on edges could avoid huge temperature drop
                # end iteration if total network temperature drop is higher than 60 K
                print('cannot fulfill substation supply node temperature requirement after iterations:',
                      iteration, abs(d_t).min())
                node_insufficient = d_t[d_t < 0].index.values
                for node in range(node_insufficient.size):
                    index_insufficient = np.argwhere(edge_node_df.index == node_insufficient[node])[0]
                    t_node[index_insufficient] = t_target_supply__c[index_insufficient] + 273.15
                    # force setting node temperature to target to avoid substation HEX calculation error.
                    # However, it might potentially cause error at mass flow iteration.
                flag = 1
            else:
                flag = 1
        else:  # when network type == 'DC'
            # calculate the difference between node temperature and the target supply temperature at substations
            # [K] temperature differences b/t node supply and target supply
            d_t = (t_node - (t_target_supply__c + 273.15)).dropna()

            # enter iteration if the node supply temperature is higher than the target supply temperature
            # (0.1 is the tolerance)
            if all(d_t < 0.1) == False and (t_plant_sup_0 - t_plant_sup) < 10:
                # increase plant supply temperature and re-iterate the node supply temperature calculation
                # increase by the maximum amount of temperature deficit at nodes
                t_plant_sup = t_plant_sup - abs(d_t.max())
                z_note = z.copy()
                t_e_out = z_pipe_out.copy()
                t_e_in = z_pipe_in.copy().dot(-1)
                t_node = np.zeros(z.shape[0])
                iteration += 1
            elif all(d_t < 0.1) == False and (t_plant_sup_0 - t_plant_sup) >= 10:
                # end iteration if total network temperature rise is higher than 10 K
                print('cannot fulfill substation supply node temperature requirement after iterations:',
                      iteration, d_t.min())
                node_insufficient = d_t[d_t > 0].index.values
                for node in range(node_insufficient.size):
                    index_insufficient = np.argwhere(edge_node_df.index == node_insufficient[node])[0]
                    t_node[index_insufficient] = t_target_supply__c[index_insufficient] + 273.15
                    # force setting node temperature to target to avoid substation HEX calculation error.
                    # However, it might potentially cause error at mass flow iteration.
                    flag = 1
            else:
                flag = 1

    # calculate pipe heat losses
    q_loss_edges_kw = np.zeros(z_note.shape[1])
    for edge in range(z_note.shape[1]):
        if m_d[edge, edge] > 0:
            dT_edge = np.nanmax(t_e_in[:, edge]) - np.nanmax(t_e_out[:, edge])
            q_loss_edges_kw[edge] = m_d[edge, edge] * gv.cp/1000 * dT_edge  # kW

    return t_node.T, plant_node, q_loss_edges_kw


def calculate_outflow_temp(z, z_note, m_d, t_e_out, z_pipe_out, t_node, t_e_in, t_ground_k, not_stuck, k, gv):
    """
    calculates outflow temperature of nodes based on incoming mass flows and temperatures.

    :param z: copy of edge-node matrix (n x e)
    :param z_note: copy of z matrix (n x e)
    :param m_d: pipe mass flow rate matrix (e x e)
    :param t_e_out: storage for outflow temperatures (n x e)
    :param z_pipe_out: matrix storing only outflow index (n x e)
    :param t_node: node temperature vector (n x 1)
    :param t_e_in: storage for inflow temperatures (n x e)
    :param t_ground_k: ground temperature over time
    :param not_stuck: vector indicating if we are stuck in a looped network and need iteration (1 x e)
    :param k: thermal coefficient for heat transfer (1 x e)
    :param gv: global variable

    :type z: dataframe (n x e)
    :type z_note: dataframe(n x e)
    :type m_d: dataframe(e x e)
    :type t_e_out: dataframe (n x e)
    :type z_pipe_out: dataframe (n x e)
    :type t_node: ndarray (n x 1)
    :type t_e_in: dataframe (n x e)
    :type t_ground_k: dataframe
    :type not_stuck: ndarray (1 x e)
    :type k: ndarray (1 x e)
    :type gv: param

    :return z: copy of edge-node matrix (n x e)
    :return z_note: copy of z matrix (n x e)
    :return m_d: pipe mass flow rate matrix (e x e)
    :return t_e_out: storage for outflow temperatures (n x e)
    :return z_pipe_out: matrix storing only outflow index (n x e)
    :return t_node: node temperature vector (n x 1)
    :return t_e_in: storage for inflow temperatures (n x e)
    :return t_ground_k: ground temperature over time
    :return not_stuck: vector indicating if we are stuck in a looped network and need iteration (1 x e)

    :rtype z: dataframe (n x e)
    :rtype z_note: dataframe(n x e)
    :rtype m_d: dataframe(e x e)
    :rtype t_e_out: dataframe (n x e)
    :rtype z_pipe_out: dataframe (n x e)
    :rtype t_node: ndarray (n x 1)
    :rtype t_e_in: dataframe (n x e)
    :rtype t_ground_k: dataframe
    :rtype not_stuck: ndarray (1 x e)

    """
    # we get not_stuck because we have a loop with no intuitive place to start. Iteration is necessary
    for j in range(z.shape[0]):
        # check if all inlet flow info towards node j are known (only -1 left in row Z_note[j])
        if np.count_nonzero(z_note[j] == 1) == 0 and np.count_nonzero(z_note[j] == 0) != z.shape[1]:
            # calculate node temperature with merging flows from pipes
            part1 = np.dot(m_d, t_e_out[j]).sum()  # sum of massflows entering node * Entry Temperature
            part2 = np.dot(m_d, z_pipe_out[j]).sum()  # total massflow leaving node
            t_node[j] = part1 / part2
            if t_node[j] == np.nan:
                raise ValueError('The are no flow entering/existing ', z.index[j],
                                 '. Please check if the edge_node_df make sense.')
            # write the node temperature to the corresponding pipe inlet
            t_e_in[j] = t_e_in[j] * t_node[j]

            # calculate pipe outlet temperatures entering from node j
            for edge in range(z_note.shape[1]):
                # find the pipes with water flow leaving from node j
                if t_e_in[j, edge] != 0:
                    # calculate the pipe outlet temperature entering from node j
                    calc_t_out(j, edge, k, m_d, z, t_e_in, t_e_out, t_ground_k, z_note, gv)
            not_stuck[j] = True
        # fill in temperatures for nodes at network branch ends
        elif t_node[j] == 0 and t_e_out[j].max() != 1:
            t_node[j] = np.nan if np.isnan(t_e_out[j]).any() else t_e_out[j].max()
            not_stuck[j] = True
        elif t_e_out[j].min() < 0:
            print('negative node temperature!')
            not_stuck[j] = True
        else:
            not_stuck[j] = False

    return z, z_note, m_d, t_e_out, z_pipe_out, t_node, t_e_in, t_ground_k, not_stuck


def calc_return_temperatures(gv, t_ground, edge_node_df, mass_flow_df, mass_flow_substation_df, k, t_return):
    """
    This function calculates the node temperatures considering heat losses in the return line.
    Starting from the substations at the end branches, the function goes through the edge-node index to search for the
    outlet node, and calculates the outlet node temperature after heat loss. Starting from that outlet node, the function
    calculates the node temperature at the corresponding pipe outlet, and the calculation goes on until all the node
    temperatures are solved. At nodes connecting to multiple pipes, the mixing temperature is calculated.

    :param gv: an instance of globalvar.GlobalVariables with the constants  to use (like `list_uses` etc.)
    :param t_ground: vector with ground temperatures in K
    :param edge_node_df: DataFrame consisting of n rows (number of nodes) and e columns (number of edges)
                        and indicating the direction of flow of each edge e at node n: if e points to n,
                        value is 1; if e leaves node n, -1; else, 0.
    :param mass_flow_df: DataFrame containing the mass flow rate for each edge e at each t
    :param mass_flow_substation_df: DataFrame containing the mass flow rate for each substation at each t
    :param k: aggregated heat conduction coefficient for each pipe
    :param t_return: return temperatures at the substations

    :return t_node.T: list of node temperatures (nx1)
    :rtype t_node.T: list

    """

    z = np.asarray(edge_node_df) * (-1)  # (n x e) edge-node matrix
    z_pipe_out = z.clip(min=0)  # pipe outlet matrix
    z_pipe_in = z.clip(max=0)  # pipe inlet matrix

    m_sub = np.zeros((z.shape[0], z.shape[0]))  # (nxn) substation flow rate matrix
    np.fill_diagonal(m_sub, mass_flow_substation_df)

    m_d = np.zeros((z.shape[1], z.shape[1]))  # (exe) pipe mass flow rate matrix
    np.fill_diagonal(m_d, mass_flow_df)

    # matrices to store results
    t_e_out = z_pipe_out.copy()
    t_node = np.zeros(z.shape[0])

    # same as for supply temperatures this vector stores information on if we are stuck while calculating looped
    # networks and need to begin iteration with an initial guess
    not_stuck = np.array([True] * z.shape[0])
    # count iterations
    temp_iter = 0
    # temperature tolerance for convergence
    temp_tolerance = 1
    # some initial value larger than the tolerance
    delta_temp_0 = 2

    while delta_temp_0 >= temp_tolerance:
        t_e_out_old = np.array(t_e_out)
        # reset_matrixes
        z_note = z.copy()
        t_e_out = z_pipe_out.copy()
        t_e_in = z_pipe_in.copy().dot(-1)
        t_node = np.zeros(z.shape[0])
        m_sub = np.zeros((z.shape[0], z.shape[0]))  # (nxn) substation flow rate matrix
        np.fill_diagonal(m_sub, mass_flow_substation_df)

        # calculate the return pipe node temperature of substations locating at the end of the branch
        for i in range(z.shape[0]):
            # choose the consumer nodes locating at the end of the branches
            if np.count_nonzero(z[i] == 1) == 0 and np.count_nonzero(z[i] == 0) != z.shape[1]:
                t_node[i] = t_return.values[0, i]
                # t_node[i] = map(list, t_return.values)[0][i]
                for edge in range(z_note.shape[1]):
                    if t_e_in[i, edge] != 0:
                        t_e_in[i, edge] = map(list, t_return.values)[0][i]
                        # calculate pipe outlet
                        calc_t_out(i, edge, k, m_d, z, t_e_in, t_e_out, t_ground, z_note, gv)

        while z_note.max() >= 1:
            if not_stuck.any():
                for j in range(z.shape[0]):
                    if np.count_nonzero(z_note[j] == 1) == 0 and np.count_nonzero(z_note[j] == 0) != z.shape[1]:
                        # calculate node temperature with merging flows from pipes
                        t_node[j] = calc_return_node_temperature(j, m_d, t_e_out, t_return, z_pipe_out, m_sub)
                        for edge in range(z_note.shape[1]):
                            if t_e_in[j, edge] != 0:
                                t_e_in[j, edge] = t_node[j]
                                # calculate pipe outlet
                                calc_t_out(j, edge, k, m_d, z, t_e_in, t_e_out, t_ground, z_note, gv)
                        not_stuck[j] = True
                    elif np.argwhere(z_note[j] == 0).size == z.shape[1] and t_node[j] == 0:
                        t_node[j] = calc_return_node_temperature(j, m_d, t_e_out, t_return, z_pipe_out, m_sub)
                        not_stuck[j] = True
                    else:
                        not_stuck[j] = False

            else:  # we got stuck because we have loops, we need an initial value
                for k in range(np.shape(t_e_out)[1]):
                    if np.any(t_e_out[:, k] == 1):
                        z_note[np.where(t_e_out[:, k] == 1), k] = 0  # remove inflow value from Z_note
                        if temp_iter < 1:
                            t_e_out[np.where(t_e_out[:, k] == 1), k] = t_return.values[
                                0, k]  # assume some node temperature
                        else:
                            t_e_out[np.where(t_e_out[:, k] == 1), k] = t_e_out_old[
                                np.where(t_e_out[:, k] == 1), k]  # iterate
                        break
                not_stuck = np.array([True] * z.shape[0])

        # calculate temperature with merging flows from pipes at the plant node
        if len(np.where(t_node == 0)[0]) != 0:
            node_index = np.where(t_node == 0)[0][0]
            m_sub[node_index] = 0
            t_node[node_index] = calc_return_node_temperature(node_index, m_d, t_e_out, t_return,
                                                              z_pipe_out, m_sub)

        delta_temp_0 = np.max(abs(t_e_out_old - t_e_out))
        temp_iter = temp_iter + 1

    return t_node


def calc_return_node_temperature(index, m_d, t_e_out, t_return, z_pipe_out, m_sub):
    """
    The function calculates the node temperature with merging flows from pipes in the return line.

    :param index: node index
    :param m_d: pipe mass flow matrix (exe)
    :param t_e_out: pipe outlet temperatures in edge node matrix (nxe)
    :param t_return: list of substation return temperatures
    :param z_pipe_out: pipe outlet matrix (nxe)
    :param m_sub: DataFrame substation flow rate

    :type index: floatT_return_all_2
    :type m_d: DataFrame
    :type t_e_out: DataFrame
    :type t_return: list
    :type z_pipe_out: DataFrame
    :type m_sub: DataFrame

    :returns t_node: node temperature with merging flows in the return line
    :rtype t_node: float

    """
    total_mass_flow_to_node = np.dot(m_d, z_pipe_out[index]).sum() + m_sub[index].max()
    if total_mass_flow_to_node == 0:
        # set node temperature to nan if no flow to node
        t_node = np.nan
    else:
        total_mcp_from_edges = np.dot(m_d, np.nan_to_num(t_e_out[index])).sum()
        total_mcp_from_substations = 0 if m_sub[index].max() == 0 else np.dot(m_sub[index].max(),
                                                                              t_return.values[0, index])
        t_node = (total_mcp_from_edges + total_mcp_from_substations) / total_mass_flow_to_node
    return t_node


def calc_t_out(node, edge, k, m_d, z, t_e_in, t_e_out, t_ground, z_note, gv):
    """
    Given the pipe inlet temperature, this function calculate the outlet temperature of the pipe.
    Following the reference of [Wang et al., 2016]_.

    :param node: node index
    :param edge: edge indices
    :param k: DataFrame of aggregated heat conduction coefficient for each pipe (exe)
    :param m_d: DataFrame of pipe flow rate (exe)
    :param z: DataFrame of  edge_node_matrix (nxe)
    :param t_e_in: DataFrame of pipe inlet temperatures [K] in edge_node_matrix (nxe)
    :param t_e_out: DataFrame of  pipe outlet temperatures [K] in edge_node_matrix (nxe)
    :param t_ground: vector with ground temperatures in [K]
    :param z_note: DataFrame of the matrix to store information of solved nodes
    :param gv: an instance of globalvar.GlobalVariables with the constants  to use (like `list_uses` etc.)

    :type node: float
    :type edge: np array
    :type k: DataFrame
    :type m_d: DataFrame
    :type z: DataFrame
    :type t_e_in: DataFrame
    :type t_e_out: DataFrame
    :type t_ground: list
    :type z_note: DataFrame
    :type gv: GlobalVariables

    :returns The calculated pipe outlet temperatures are directly written to T_e_out

    ..[Wang et al, 2016] Wang J., Zhou, Z., Zhao, J. (2016). A method for the steady-state thermal simulation of
    district heating systems and model parameters calibration. Energy Conversion and Management, 120, 294-305.
    """
    # calculate pipe outlet temperature
    if isinstance(edge, np.ndarray) == False:
        edge = np.array([edge])

    m_d = np.round(m_d, decimals = 5) #round to avoid errors at very very low massflows

    for i in range(edge.size):
        e = edge[i]
        k = k[e, e]
        m = m_d[e, e]
        out_node_index = np.where(z[:, e] == 1)[0].max()
        if abs(m) == 0 and z[node, e] == -1:
            # set outlet temperature to nan if no flow is going out from node to connected edges
            t_e_out[out_node_index, e] = np.nan
            z_note[:, e] = 0

        elif z[node, e] == -1:
            # calculate outlet temperature if flow goes from node to out_node through edge
            t_e_out[out_node_index, e] = (t_e_in[node, e] * (k / 2 - m * gv.cp/1000) - k * t_ground) / (
                    -m * gv.cp/1000 - k / 2)  # [K]
            dT = t_e_in[node, e] - t_e_out[out_node_index, e]
            if abs(dT) > 30:
                print('High temperature loss on edge', e, '. Loss:', abs(dT))
                if (k / 2 - m * gv.cp/1000) > 0:
                    print(
                        'Exit temperature decreasing at entry temperature increase. Possible at low massflows. Massflow:',
                        m, ' on edge: ', e)
                    t_e_out[out_node_index, e] = t_e_in[node, e] - 30  # assumes maximum 30 K temperature loss
                    # Induces some error but necessary to avoid spiraling to negative temperatures
                    # Todo: find better method which allows loss calculation at low massflows
            z_note[:, e] = 0


def calc_aggregated_heat_conduction_coefficient(mass_flow, locator, gv, edge_df, pipe_properties_df, temperature__k,
                                                network_type):
    """
    This function calculates the aggregated heat conduction coefficients of all the pipes.
    Following the reference from [Wang et al., 2016].
    The pipe material properties are referenced from _[A. Kecabas et al., 2011], and the pipe catalogs are referenced
    from _[J.A. Fonseca et al., 2016] and _[isoplus].

    :param mass_flow: Vector with mass flows of each edge                           (e x 1)
    :param locator: an InputLocator instance set to the scenario to work on
    :param gv: an instance of globalvar.GlobalVariables with the constants  to use (like `list_uses` etc.)
    :param pipe_properties_df: DataFrame containing the pipe properties for each edge in the network
    :param temperature__k: matrix containing the temperature of the water in each edge e at time t             (t x e)
    :param gv: an instance of globalvar.GlobalVariables with the constants  to use (like `list_uses` etc.)
    :param network_type: a string that defines whether the network is a district heating ('DH') or cooling ('DC')
                         network
    :param edge_df: list of edges and their corresponding lengths and start and end nodes

    :type temperature__k: list
    :type gv: GlobalVariables
    :type network_type: str
    :type mass_flow: DataFrame
    :type locator: InputLocator
    :type gv: GlobalVariables
    :type pipe_properties_df: DataFrame
    :type edge_df: DataFrame

    :return k_all: DataFrame of aggregated heat conduction coefficients (1 x e) for all edges

    ..[Wang et al, 2016] Wang J., Zhou, Z., Zhao, J. (2016). A method for the steady-state thermal simulation of
      district heating systems and model parameters calibration. Eenergy Conversion and Management, 120, 294-305.

    ..[A. Kecebas et al., 2011] A. Kecebas et al. Thermo-economic analysis of pipe insulation for district heating
      piping systems. Applied Thermal Engineering, 2011.

    ..[J.A. Fonseca et al., 2016] J.A. Fonseca et al. City Energy Analyst (CEA): Integrated framework for analysis and
      optimization of building energy systems in neighborhoods and city districts. Energy and Buildings. 2016

    ..[isoplus] isoplus piping systems. http://en.isoplus.dk/download-centre

    .. Incropera, F. P., DeWitt, D. P., Bergman, T. L., & Lavine, A. S. (2007). Fundamentals of Heat and Mass
       Transfer. Fundamentals of Heat and Mass Transfer. https://doi.org/10.1016/j.applthermaleng.2011.03.022
    """

    L_pipe = edge_df['pipe length']
    material_properties = pd.read_excel(locator.get_thermal_networks(), sheetname=['MATERIAL PROPERTIES'])[
        'MATERIAL PROPERTIES']
    material_properties = material_properties.set_index(material_properties['material'].values)
    conductivity_pipe = material_properties.ix['Steel', 'lamda_WmK']  # _[A. Kecebas et al., 2011]
    conductivity_insulation = material_properties.ix['PUR', 'lamda_WmK']  # _[A. Kecebas et al., 2011]
    conductivity_ground = material_properties.ix['Soil', 'lamda_WmK']  # _[A. Kecebas et al., 2011]
    network_depth = gv.NetworkDepth  # [m]
    extra_heat_transfer_coef = 0.2  # _[Wang et al, 2016] to represent heat losses from valves and other attachments

    # calculate nusselt number
    nusselt = calc_nusselt(mass_flow, gv, temperature__k, pipe_properties_df[:]['D_int_m':'D_int_m'].values[0],
                           network_type)
    # calculate thermal conductivity of water
    thermal_conductivity = calc_thermal_conductivity(temperature__k)
    # evaluate thermal heat transfer coefficient
    alpha_th = thermal_conductivity * nusselt / pipe_properties_df[:]['D_int_m':'D_int_m'].values[0]  # W/(m^2 * K)

    k_all = []
    for pipe_number, pipe in enumerate(L_pipe.index):
        # calculate heat resistances, equation (3) in Wang et al., 2016
        R_pipe = np.log(pipe_properties_df.loc['D_ext_m', pipe] / pipe_properties_df.loc['D_int_m', pipe]) / (
                2 * math.pi * conductivity_pipe)  # [m*K/W]
        R_insulation = np.log((pipe_properties_df.loc['D_ins_m', pipe]) / pipe_properties_df.loc['D_ext_m', pipe]) / (
                2 * math.pi * conductivity_insulation)  # [m*K/W]
        a = 2 * network_depth / (pipe_properties_df.loc['D_ins_m', pipe])
        R_ground = np.log(a + (a ** 2 - 1) ** 0.5) / (2 * math.pi * conductivity_ground)  # [m*K/W]
        # calculate convection heat transfer resistance
        if alpha_th[pipe_number] == 0:
            R_conv = 0.2  # case with no massflow, avoids divide by 0 error
        else:
            R_conv = 1 / (
                    alpha_th[pipe_number] * math.pi * pipe_properties_df[:]['D_int_m':'D_int_m'].values[0][pipe_number])
        # calculate the aggregated heat conduction coefficient, equation (4) in Wang et al., 2016
        k = L_pipe[pipe] * (1 + extra_heat_transfer_coef) / (R_pipe + R_insulation + R_ground + R_conv) / 1000  # [kW/K]
        k_all.append(k)
    k_all = np.diag(k_all)
    return k_all


def calc_nusselt(mass_flow_rate_kgs, gv, temperature_K, pipe_diameter_m, network_type):
    """
    Calculates the nusselt number of the internal flow inside the pipes.

    :param pipe_diameter_m: vector containing the pipe diameter in m for each edge e in the network           (e x 1)
    :param mass_flow_rate_kgs: matrix containing the mass flow rate in each edge e at time t                    (t x e)
    :param temperature_K: matrix containing the temperature of the water in each edge e at time t             (t x e)
    :param gv: an instance of globalvar.GlobalVariables with the constants  to use (like `list_uses` etc.)
    :param network_type: a string that defines whether the network is a district heating ('DH') or cooling ('DC')
                         network
    :type pipe_diameter_m: ndarray
    :type mass_flow_rate_kgs: ndarray
    :type temperature_K: list
    :type gv: GlobalVariables
    :type network_type: str

    :return nusselt: calculated nusselt number for flow in each edge		(ex1)
    :rtype nusselt: ndarray

	.. Incropera, F. P., DeWitt, D. P., Bergman, T. L., & Lavine, A. S. (2007).
	Fundamentals of Heat and Mass Transfer. Fundamentals of Heat and Mass Transfer.
	https://doi.org/10.1016/j.applthermaleng.2011.03.022
    """

    # calculate variable values necessary for nusselt number evaluation
    reynolds = calc_reynolds(mass_flow_rate_kgs, gv, temperature_K, pipe_diameter_m)
    prandtl = calc_prandtl(gv, temperature_K)
    darcy = calc_darcy(pipe_diameter_m, reynolds, gv.roughness)

    nusselt = np.zeros(reynolds.size)
    for rey in range(reynolds.size):
        if reynolds[rey] <= 1:
            # calculate nusselt number only if mass is flowing
            nusselt[rey] = 0
        elif reynolds[rey] <= 2300:
            # calculate the Nusselt number for laminar flow
            nusselt[rey] = 3.66
        elif reynolds[rey] <= 10000:
            # calculate the Nusselt for transient flow
            nusselt[rey] = darcy[rey] / 8 * (reynolds[rey] - 1000) * prandtl[rey] / (
                    1 + 12.7 * (darcy[rey] / 8) ** 0.5 * (prandtl[rey] ** 0.67 - 1))
        else:
            # calculate the Nusselt number for turbulent flow
            # identify if heating or cooling case
            if network_type == 'DH':  # warm fluid, so ground is cooling fluid in pipe, cooling case from view of thermodynamic flow
                nusselt[rey] = 0.023 * reynolds[rey] ** 0.8 * prandtl[rey] ** 0.3
            else:
                # cold fluid, so ground is heating fluid in pipe, heating case from view of thermodynamic flow
                nusselt[rey] = 0.023 * reynolds[rey] ** 0.8 * prandtl[rey] ** 0.4

    return nusselt


# ============================
# Other functions
# ============================


def get_thermal_network_from_csv(locator, network_type, network_name):
    """
    This function reads the existing node and pipe network from csv files (as provided for the Zug reference case) and
    produces an edge-node incidence matrix (as defined by Oppelt et al., 2016) as well as the length of each edge.

    :param locator: an InputLocator instance set to the scenario to work on
    :param network_type: a string that defines whether the network is a district heating ('DH') or cooling ('DC')
                         network
    :type locator: InputLocator
    :type network_type: str

    :return edge_node_df: DataFrame consisting of n rows (number of nodes) and e columns (number of edges) and
                        indicating direction of flow of each edge e at node n: if e points to n, value is 1; if
                        e leaves node n, -1; else, 0.                                                           (n x e)
    :return all_nodes_df: DataFrame that contains all nodes, whether a node is a consumer, plant, or neither,
                        and, if it is a consumer or plant, the name of the corresponding building               (2 x n)
    :return pipe_data_df['LENGTH']: vector containing the length of each edge in the network                    (1 x e)
    :rtype edge_node_df: DataFrame
    :rtype all_nodes_df: DataFrame
    :rtype pipe_data_df['LENGTH']: array

    The following files are created by this script:
        - DH_EdgeNode: csv file containing edge_node_df stored in locator.get_optimization_network_layout_folder()
        - DH_AllNodes: csv file containing all_nodes_df stored in locator.get_optimization_network_layout_folder()

    ..[Oppelt, T., et al., 2016] Oppelt, T., et al. Dynamic thermo-hydraulic model of district cooling networks.
    Applied Thermal Engineering, 2016.

    """

    t0 = time.clock()

    # get node and pipe data
    node_df = pd.read_csv(locator.get_network_layout_nodes_csv_file(network_type)).set_index('DC_ID')
    edge_df = pd.read_csv(locator.get_network_layout_pipes_csv_file(network_type)).set_index('DC_ID')
    edge_df.rename(columns={'LENGTH': 'pipe length'},
                   inplace=True)  # todo: could be removed when the input format of .csv is fixed

    # sort dataframe with node/edge numbers
    node_sorted_index = node_df.index.to_series().str.split('J', expand=True)[1].apply(int).sort_values(
        ascending=True)
    node_df = node_df.reindex(index=node_sorted_index.index)
    edge_sorted_index = edge_df.index.to_series().str.split('PIPE', expand=True)[1].apply(int).sort_values(
        ascending=True)
    edge_df = edge_df.reindex(index=edge_sorted_index.index)

    # create consumer and plant node vectors from node data
    for column in ['Plant', 'Sink']:
        if type(node_df[column][0]) != int:
            node_df[column] = node_df[column].astype(int)
    node_names = node_df.index.values
    consumer_nodes = np.vstack((node_names, (node_df['Sink'] * node_df['Name']).values))
    plant_nodes = np.vstack((node_names, (node_df['Plant'] * node_df['Name']).values))

    # create edge-node matrix from pipe data
    list_edges = edge_df.index.values
    list_nodes = node_df.index.values
    edge_node_matrix = np.zeros((len(list_nodes), len(list_edges)))
    for j in range(len(list_edges)):
        for i in range(len(list_nodes)):
            if edge_df['NODE2'][j] == list_nodes[i]:
                edge_node_matrix[i][j] = 1
            elif edge_df['NODE1'][j] == list_nodes[i]:
                edge_node_matrix[i][j] = -1
    edge_node_df = pd.DataFrame(data=edge_node_matrix, index=list_nodes, columns=list_edges)
    edge_node_df.to_csv(locator.get_optimization_network_edge_node_matrix_file(network_type, network_name))

    all_nodes_df = pd.DataFrame(index=list_nodes, columns=['Building', 'Type'])
    for i in range(len(list_nodes)):
        if consumer_nodes[1][i] != '':
            all_nodes_df.loc[list_nodes[i], 'Building'] = consumer_nodes[1][i]
            all_nodes_df.loc[list_nodes[i], 'Type'] = 'CONSUMER'
        elif plant_nodes[1][i] != '':
            all_nodes_df.loc[list_nodes[i], 'Building'] = plant_nodes[1][i]
            all_nodes_df.loc[list_nodes[i], 'Type'] = 'PLANT'
        else:
            all_nodes_df.loc[list_nodes[i], 'Building'] = 'NONE'
            all_nodes_df.loc[list_nodes[i], 'Type'] = 'NONE'
    all_nodes_df.to_csv(locator.get_optimization_network_node_list_file(network_type, network_name))

    print(time.clock() - t0, "seconds process time for Network Summary\n")

    return edge_node_df, all_nodes_df, edge_df


def get_thermal_network_from_shapefile(locator, network_type, network_name):
    """
    This function reads the existing node and pipe network from a shapefile and produces an edge-node incidence matrix
    (as defined by Oppelt et al., 2016) as well as the edge properties (length, start node, and end node) and node
    coordinates.

    :param locator: an InputLocator instance set to the scenario to work on
    :param network_type: a string that defines whether the network is a district heating ('DH') or cooling ('DC')
                         network
    :param gv: path to global variables classg
    :type locator: InputLocator
    :type network_type: str

    :return edge_node_df: DataFrame consisting of n rows (number of nodes) and e columns (number of edges) and
                        indicating direction of flow of each edge e at node n: if e points to n, value is 1; if
                        e leaves node n, -1; else, 0.                                                           (n x e)
    :return all_nodes_df: DataFrame that contains all nodes, whether a node is a consumer, plant, or neither,
                        and, if it is a consumer or plant, the name of the corresponding building               (2 x n)
    :return edge_df['pipe length']: vector containing the length of each edge in the network                    (1 x e)
    :rtype edge_node_df: DataFrame
    :rtype all_nodes_df: DataFrame
    :rtype edge_df['pipe length']: array

    The following files are created by this script:
        - DH_EdgeNode: csv file containing edge_node_df stored in locator.get_optimization_network_layout_folder()
        - DH_Node_DF: csv file containing all_nodes_df stored in locator.get_optimization_network_layout_folder()
        - DH_Pipe_DF: csv file containing edge_df stored in locator.get_optimization_network_layout_folder()

    ..[Oppelt, T., et al., 2016] Oppelt, T., et al. Dynamic thermo-hydraulic model of district cooling networks.
    Applied Thermal Engineering, 2016.

    """

    t0 = time.clock()

    # import shapefiles containing the network's edges and nodes
    network_edges_df = gpd.read_file(locator.get_network_layout_edges_shapefile(network_type, network_name))
    network_nodes_df = gpd.read_file(locator.get_network_layout_nodes_shapefile(network_type, network_name))

    # check duplicated NODE/PIPE IDs
    duplicated_nodes = network_nodes_df[network_nodes_df.Name.duplicated(keep=False)]
    duplicated_edges = network_edges_df[network_edges_df.Name.duplicated(keep=False)]
    if duplicated_nodes.size > 0:
        raise ValueError('There are duplicated NODE IDs:', duplicated_nodes)
    if duplicated_edges.size > 0:
        raise ValueError('There are duplicated PIPE IDs:', duplicated_nodes)

    # get node and pipe information
    node_df, edge_df = extract_network_from_shapefile(network_edges_df, network_nodes_df)

    # create node catalogue indicating which nodes are plants and which consumers
    all_nodes_df = node_df[['Type', 'Building']]
    all_nodes_df.to_csv(locator.get_optimization_network_node_list_file(network_type, network_name))
    # extract the list of buildings in the current network
    building_names = all_nodes_df.Building[all_nodes_df.Type == 'CONSUMER'].reset_index(drop=True)

    # create first edge-node matrix
    list_pipes = edge_df.index.values
    list_nodes = node_df.index.values
    edge_node_matrix = np.zeros((len(list_nodes), len(list_pipes)))
    for j in range(len(list_pipes)):  # TODO: find ways to accelerate
        for i in range(len(list_nodes)):
            if edge_df['end node'][j] == list_nodes[i]:
                edge_node_matrix[i][j] = 1
            elif edge_df['start node'][j] == list_nodes[i]:
                edge_node_matrix[i][j] = -1
    edge_node_df = pd.DataFrame(data=edge_node_matrix, index=list_nodes, columns=list_pipes)  # first edge-node matrix

    ## An edge node matrix is generated as a first guess and then virtual substation mass flows are imposed to
    ## calculate mass flows in each edge (mass_flow_guess).
    node_mass_flows_df = pd.DataFrame(data=np.zeros([1, len(edge_node_df.index)]), columns=edge_node_df.index)
    total_flow = 0
    number_of_plants = sum(all_nodes_df['Type'] == 'PLANT')

    for node, row in all_nodes_df.iterrows():
        if row['Type'] == 'CONSUMER':
            node_mass_flows_df[node] = 1  # virtual consumer mass flow requirement
            total_flow += 1
    for node, row in all_nodes_df.iterrows():
        if row['Type'] == 'PLANT':
            node_mass_flows_df[node] = - total_flow / number_of_plants  # virtual plant supply mass flow

    # The direction of flow is then corrected
    # keep track if there was a change for the iterative process
    changed = [True] * node_mass_flows_df.shape[1]
    while any(changed):
        for i in range(node_mass_flows_df.shape[1]):
            # we have a plant with incoming mass flows, or we don't have a plant but only exiting mass flows
            if ((node_mass_flows_df[node_mass_flows_df.columns[i]].min() < 0) and (edge_node_df.iloc[i].max() > 0)) or \
                    ((node_mass_flows_df[node_mass_flows_df.columns[i]].min() >= 0) and (
                            edge_node_df.iloc[i].max() <= 0)):
                j = np.nonzero(edge_node_df.iloc[i])[0]
                if len(j) > 1:  # valid if e.g. if more than one flow and all flows incoming. Only need to flip one.
                    j = random.choice(j)
                edge_node_df[edge_node_df.columns[j]] = -edge_node_df[edge_node_df.columns[j]]
                new_nodes = [edge_df['end node'][j], edge_df['start node'][j]]
                edge_df['start node'][j] = new_nodes[0]
                edge_df['end node'][j] = new_nodes[1]
                changed[i] = True
            else:
                changed[i] = False

    # make sure there are no NONE-node at dead ends before proceeding
    plant_counter = 0
    for i in range(edge_node_df.shape[0]):
        if np.count_nonzero(
                edge_node_df.iloc[i] == 1) == 0:  # Check if only has outflowing values, if yes, it is a plant
            plant_counter += 1
    if number_of_plants != plant_counter:
        raise ValueError('Please erase ', (plant_counter - number_of_plants),
                         ' end node(s) that are neither buildings nor plants.')

    edge_node_df.to_csv(locator.get_optimization_network_edge_node_matrix_file(network_type, network_name))
    print(time.clock() - t0, "seconds process time for Network Summary\n")

    return edge_node_df, all_nodes_df, edge_df, building_names


def extract_network_from_shapefile(edge_shapefile_df, node_shapefile_df):
    """
    Extracts network data into DataFrames for pipes and nodes in the network

    :param edge_shapefile_df: DataFrame containing all data imported from the edge shapefile
    :param node_shapefile_df: DataFrame containing all data imported from the node shapefile
    :type edge_shapefile_df: DataFrame
    :type node_shapefile_df: DataFrame
    :return node_df: DataFrame containing all nodes and their corresponding coordinates
    :return edge_df: list of edges and their corresponding lengths and start and end nodes
    :rtype node_df: DataFrame
    :rtype edge_df: DataFrame

    """
    # set precision of coordinates
    decimals = 6
    # create node dictionary with plant and consumer nodes
    node_dict = {}
    node_shapefile_df.set_index("Name", inplace=True)
    node_shapefile_df = node_shapefile_df.astype('object')
    node_shapefile_df['coordinates'] = node_shapefile_df['geometry'].apply(lambda x: x.coords[0])
    # sort node_df by index number
    node_sorted_index = node_shapefile_df.index.to_series().str.split('NODE', expand=True)[1].apply(int).sort_values(
        ascending=True)
    node_shapefile_df = node_shapefile_df.reindex(index=node_sorted_index.index)
    # assign node properties (plant/consumer/none)
    node_shapefile_df['plant'] = ''
    node_shapefile_df['consumer'] = ''
    node_shapefile_df['none'] = ''

    for node, row in node_shapefile_df.iterrows():
        coord_node = row['geometry'].coords[0]
        if row['Type'] == "PLANT":
            node_shapefile_df.loc[node, 'plant'] = node
        elif row['Type'] == "CONSUMER":  # TODO: add 'PROSUMER' by splitting nodes
            node_shapefile_df.loc[node, 'consumer'] = node
        else:
            node_shapefile_df.loc[node, 'none'] = node
        coord_node_round = (round(coord_node[0], decimals), round(coord_node[1], decimals))
        node_dict[coord_node_round] = node

    # create edge dictionary with pipe lengths and start and end nodes
    # complete node dictionary with missing nodes (i.e., joints)
    edge_shapefile_df.set_index("Name", inplace=True)
    edge_shapefile_df = edge_shapefile_df.astype('object')
    edge_shapefile_df['coordinates'] = edge_shapefile_df['geometry'].apply(lambda x: x.coords[0])
    # sort edge_df by index number
    edge_sorted_index = edge_shapefile_df.index.to_series().str.split('PIPE', expand=True)[1].apply(int).sort_values(
        ascending=True)
    edge_shapefile_df = edge_shapefile_df.reindex(index=edge_sorted_index.index)
    # assign edge properties
    edge_shapefile_df['pipe length'] = 0
    edge_shapefile_df['start node'] = ''
    edge_shapefile_df['end node'] = ''

    for pipe, row in edge_shapefile_df.iterrows():
        # get the length of the pipe and add to dataframe
        edge_shapefile_df.loc[pipe, 'pipe length'] = row['geometry'].length
        # get the start and end notes and add to dataframe
        edge_coords = row['geometry'].coords
        start_node = (round(edge_coords[0][0], decimals), round(edge_coords[0][1], decimals))
        end_node = (round(edge_coords[1][0], decimals), round(edge_coords[1][1], decimals))
        if start_node in node_dict.keys():
            edge_shapefile_df.loc[pipe, 'start node'] = node_dict[start_node]
        else:
            print('The start node of ', pipe, 'has no match in node_dict, check precision of the coordinates.')
        if end_node in node_dict.keys():
            edge_shapefile_df.loc[pipe, 'end node'] = node_dict[end_node]
        else:
            print('The end node of ', pipe, 'has no match in node_dict, check precision of the coordinates.')

    # # If a consumer node is not connected to the network, find the closest node and connect them with a new edge
    # # this part of the code was developed for a case in which the node and edge shapefiles were not defined
    # # consistently. This has not been a problem after all, but it could eventually be a useful feature.
    # for node in node_dict:
    #     if node not in pipe_nodes:
    #         min_dist = 1000
    #         closest_node = pipe_nodes[0]
    #         for pipe_node in pipe_nodes:
    #             dist = ((node[0] - pipe_node[0])**2 + (node[1] - pipe_node[1])**2)**.5
    #             if dist < min_dist:
    #                 min_dist = dist
    #                 closest_node = pipe_node
    #         j += 1
    #         edge_dict['EDGE' + str(j)] = [min_dist, node_dict[closest_node][0], node_dict[node][0]]

    return node_shapefile_df, edge_shapefile_df


def write_substation_values_to_nodes_df(all_nodes_df, df_value):
    """
    The function writes values (temperatures or mass flows) from each substations to the corresponding nodes in the
    edge node matrix.

    :param all_nodes_df: DataFrame containing all nodes and whether a node n is a consumer or plant node
                        (and if so, which building that node corresponds to), or neither.                   (2 x n)
    :param df_value: DataFrame of value of each substation
    :param flag: flag == True if the values are temperatures ; flag == False if the value is mass flow

    :return nodes_df: DataFrame with values at each node (1xn)
    :rtype nodes_df: DataFrame

    """

    nodes_df = pd.DataFrame(index=[0], columns=all_nodes_df.index)
    # it is assumed that if there is more than one plant, they all supply the same amount of heat at each time step
    # (i.e., the amount supplied by each plant is not optimized)
    number_of_plants = sum(all_nodes_df['Type'] == 'PLANT')
    consumer_list = all_nodes_df.loc[all_nodes_df['Type'] == 'CONSUMER', 'Building'].values
    plant_mass_flow = df_value[consumer_list].loc[0].sum() / number_of_plants

    # write all flow rates into nodes DataFrame
    ''' NOTE:
            for each node, (mass incoming edges) + (mass supplied) = (mass outgoing edges) + (mass demand)
                           (mass incoming edges) - (mass outgoing edges) = (mass demand) - (mass supplied)
            which equals   (edge node matrix) * (mass flow edge) = (mass demand) - (mass supplied)
                           (edge node matrix) * (mass flow edge) = (mass flow node)

            so mass_flow_node[node] = mass_flow_demand[node] for consumer nodes and
               mass_flow_node[node] = - mass_flow_supply[node] for plant nodes
            (i.e., mass flow is positive if it's a consumer node, negative if it's a supply node)

            assuming only one plant node, the mass flow on the supply side needs to equal the mass flow from consumers
            so mass_flow_supply = - sum(mass_flow_demand[node]) for all nodes

            for the case where there is more than one supply plant, it is assumed for now that all plants supply the
            same share of the total demand of the network
            so mass_flow_supply = - sum(mass_flow_demand)/(number of plants)
        '''

    # assure only mass flow at network consumer substations are counted
    for node, row in all_nodes_df.iterrows():
        if row['Type'] == 'CONSUMER':
            nodes_df[node] = df_value[row['Building']]
        elif row['Type'] == 'PLANT':
            nodes_df[node] = - plant_mass_flow
        else:
            nodes_df[node] = 0
    return nodes_df


def write_substation_temperatures_to_nodes_df(all_nodes_df, df_value):
    """
    The function writes values (temperatures or mass flows) from each substations to the corresponding nodes in the
    edge node matrix.

    :param all_nodes_df: DataFrame containing all nodes and whether a node n is a consumer or plant node
                        (and if so, which building that node corresponds to), or neither.                   (2 x n)
    :param df_value: DataFrame of value of each substation
    :param flag: flag == True if the values are temperatures ; flag == False if the value is mass flow

    :return nodes_df: DataFrame with values at each node (1xn)
    :rtype nodes_df: DataFrame

    """

    nodes_df = pd.DataFrame()
    # write temperature into nodes DataFrame
    for node, row in all_nodes_df.iterrows():
        if row['Type'] == 'CONSUMER':
            nodes_df[node] = df_value[row['Building']]
        else:
            nodes_df[node] = np.nan  # set temperature value to nan for non-substation nodes

    return nodes_df


def read_properties_from_buildings(building_names, buildings_demands, property):
    """
    The function reads certain property from each building and output as a DataFrame.

    :param building_names: list of building names in the scenario
    :param buildings_demands: demand of each building in the scenario
    :param property: certain property from the building demand file. e.g. T_supply_target

    :return property_df: DataFrame of the particular property at each building.
    :rtype property_df: DataFrame

    """

    property_df = pd.DataFrame(index=range(8760), columns=building_names)
    for name in building_names:
        property_per_building = buildings_demands[(building_names == name).argmax()][property]
        property_df[name] = property_per_building
    return property_df


# ============================
# test
# ============================


def main(config):
    """
    run the whole network summary routine
    """
    start = time.time()
    gv = cea.globalvar.GlobalVariables()
    locator = cea.inputlocator.InputLocator(scenario=config.scenario)

    # add options for data sources: heating or cooling network, csv or shapefile
    network_type = config.thermal_network.network_type  # set to either 'DH' or 'DC'
    file_type = config.thermal_network.file_type  # set to csv or shapefile
    set_diameter = config.thermal_network.set_diameter  # this does a rule of max and min flow to set a diameter. if false it takes the input diameters
    list_network_name = config.thermal_network.network_name

    print('Running thermal_network for scenario %s' % config.scenario)
    print('Running thermal_network for network type %s' % network_type)
    print('Running thermal_network for file type %s' % file_type)
    print('Running thermal_network for network %s' % list_network_name)

    if len(list_network_name) == 0:
        network_name = ''
        thermal_network_main(locator, gv, network_type, network_name, file_type, set_diameter)
    else:
        for network_name in list_network_name:
            thermal_network_main(locator, gv, network_type, network_name, file_type, set_diameter)
    print('test thermal_network_main() succeeded')
    print('total time: ', time.time() - start)


if __name__ == '__main__':
    main(cea.config.Configuration())