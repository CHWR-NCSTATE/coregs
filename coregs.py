import argparse
import calendar
import csv
import pathlib
import re
import shutil
import sqlite3
import sys
import os
import glob
from collections import defaultdict
from datetime import datetime, timedelta
from time import process_time as timer

import colorama
import numpy as np
import pandas as pd
from colorama import Fore, Style
from pyomo.environ import Constraint, Suffix
from IPython import embed as II

colorama.init(autoreset=True)

# insert temoa at the front of our path so we can import it
sys.path.insert(0, "../temoa/temoa_model")

import pformat_results
import temoa_model as temoa
import temoa_run

from graps_interface import GRAPS


def parse_args(input_args=None):
    """Used to parse command line arguments

    Returns:
        dict -- Dictionary containing key,value pairs for cmd line args
    """
    parser = argparse.ArgumentParser(
        description="Run temoa and GRAPS in an iterative scheme to optimize systems.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "start",
        metavar="start",
        type=str,
        help="Starting date of modeled period 'YYYY'|'YYYY-MM'",
    )
    parser.add_argument(
        "n_users",
        metavar="num_users",
        type=int,
        help="Number of users to pass to GRAPS (# user )",
    )
    parser.add_argument(
        "method",
        metavar="method",
        type=str,
        help="Which optimization method to be used.",
        choices=("icorps", "mhb", "mhp", "single")
    )
    parser.add_argument(
        "--rolling",
        action="store_true",
        help="Flag to perform a year long rolling horizon run."
    )
    parser.add_argument(
        "--one_run",
        action="store_true",
        help="For use with the rolling flag. Only runs the specified scenario but initializes with data"
             " from the previous scenario (chronologically) if one exists (e.g., the run starting in 2007-07"
             " would use data from the 2007-06 scenario just as if it was a rolling horizon approach."
    )
    parser.add_argument(
        "-E",
        "--epsilon",
        default=0.005,
        help="Option to provide an epislon (stopping criteria) value to the solver.\nThis value is should be a decimal representing the percent change you are comfortable with.",
    )
    parser.add_argument(
        "--alpha",
        type=float,
        default=2,
        help="Parameter to control step size for ICORPS. Larger numbers equal smaller step size.",
    )
    parser.add_argument("-S", "--stdout", action="store_true", help="Suppress StdOut.")

    args = parser.parse_args(input_args) if input_args else parser.parse_args()
    start_year, start_month = args.start.split("-")
    n_init_params = args.n_users * 3

    return {
        "start_year": start_year,
        "start_month": start_month,
        "n_init_params": n_init_params,
        "method": args.method,
        "epsilon": args.epsilon,
        "rolling": args.rolling,
        "first": False,
        "stdout": args.stdout,
        "alpha": args.alpha,
        "one_run": args.one_run,
    }


def get_prefix(start_month, nmonths):
    """Determine the prefix of the scenario for a
    quarterly run.

    Arguments:
        start_month {str} -- Starting month as MM
        nmonths {int} -- number of months of run

    Returns:
        str -- Month initials for the scenario (e.g "JFM")
    """
    file_prefix_values = []
    # initials for each month
    month_letters = ["J", "F", "M", "A", "M", "J", "J", "A", "S", "O", "N", "D"]
    # index by 0 but start_month = 1
    start_index = int(start_month) - 1
    # modulus allows indexes greater than 11 to work
    file_prefix_values = [
        month_letters[i % 12] for i in range(start_index, start_index + nmonths)
    ]
    return "".join(file_prefix_values)


def convert_string_to_nums(string):
    """take in a scenario prefix and return 
    the month numbers it corresponds to

    Args:
        string (str): scenario prefix (e.g "JFM")

    Returns:
        list: list of integers of months
    """
    start = string[0]
    stop = string[-1]
    # some months have the same first letter (Jan, Jun, Jul)
    # none of the preceding months first letter is the same
    # for any of the same letter months, so that is leveraged here
    months = {
        "J": {"F": 1, "J": 6, "A": 7},
        "F": 2,
        "M": {"A": 3, "J": 5},
        "A": {"M": 4, "S": 8},
        "S": 9,
        "O": 10,
        "N": 11,
        "D": 12,
    }
    # multiple months start with J, M, or A
    snum = months[start][string[1]] if start in ("J", "M", "A") else months[start]
    # length of the string is the number of months in the string
    stnum = len(string) + snum
    # when the year ends during the span
    if stnum <= 12:
        return list(range(snum, stnum))

    part1 = list(range(snum, 13))
    part2 = list(range(1, stnum - 12))
    return part1 + part2


def terminal_histogram(data_dict):
    max_value = max(data_dict.values())
    max_length = max(map(len, data_dict.keys())) + 2
    unit = "\u2588"
    print(f"{max_value:^{max_length}}: {unit*int(max_value)}")
    for key, value in sorted(data_dict.items(), key=lambda x: x[1], reverse=True):
        hist = unit * int(value)
        print(f"{key:<{max_length}}: {hist}")

def find_previous_day(year, month, day):
    return datetime(year, month, day) - timedelta(days=1)


def modify_temoa_capacity(inputFile, scenario):
    exist_cap_file = "data/existing_capacity.csv"
    df = pd.read_csv(exist_cap_file)
    con = sqlite3.connect(inputFile)
    cur = con.cursor()
    query = """
               UPDATE ExistingCapacity
               SET exist_cap = ?
               WHERE tech = ?; 
            """
    if scenario[:3] == "DJF":
        key = "winter"
    elif scenario[:3] == "JAS":
        key = "summer"
    else:
        key = ["winter", "summer"]
    key = ["nameplate"]
    for i, row in df.iterrows():
        tech = row["tech"]
        cap = row[key].mean() if type(key) == list else row[key]
        cur.execute(query, (cap, tech))
    con.commit()
    con.close()


def get_dsd(file="data/distribution.csv"):
    # TODO : Fix all paths to be more generic [file]
    """Retrieves demand specific distribution for a full year

    Keyword Arguments:
        file {str} -- File name/location of the dsd (default: {'data/distribution.csv'})

    Returns:
        dict -- Contains dsd indexed by month, day, and hour.
    """

    data = {
        str(month): {
            str(day): {str(hour): 0 for hour in range(1, 25)} for day in range(1, 31)
        }
        for month in range(1, 13)
    }

    # again, this works great for TVA, but it will not work for other systems
    # TODO
    # consider moving to pandas for compatability sake
    with open(file, "r") as f:
        for i, line in enumerate(f):
            if i != 0:
                line = line.strip("\r\n")
                month, day, hour, demand, fraction = line.split(",")
                data[month][day][hour] = float(fraction)
    return data


def modify_temoa_dsd(prefix, db_file):
    """The demand specific distribution for each state will change in 
    temoa depending on the scenario, this function ensures that it is
    correct.

    Args:
        prefix (str): scenario prefix (e.g JFM)
        db_file (str): path to the sqlite file for this scenario
    """
    distrib = get_dsd()
    # Temoa state names
    states = ["ELC_AL", "ELC_GA", "ELC_KY", "ELC_MS", "ELC_NC", "ELC_TN", "ELC_GA"]
    # connect to db and create a cursor
    con = sqlite3.connect(db_file)
    cur = con.cursor()
    query = """
        UPDATE DemandSpecificDistribution
        SET dds = ?
        WHERE t_period = ? AND season_name = ? AND
        time_of_day_name = ? AND demand_name = ?;
        """

    # if we are not running for a whole year
    if prefix != "year":
        dates_conv = convert_string_to_nums(prefix)
        for state in states:
            for month in dates_conv:
                day_dict = distrib[str(month)]
                for day, hour_dict in day_dict.items():
                    for hour, frac in hour_dict.items():
                        month_conv = "20{:d}".format(int(month) + 10)
                        cur.execute(query, (frac, int(month_conv), day, hour, state))
    # if we are running for an entire year
    else:
        for state in states:
            for month, day_dict in distrib.items():
                for day, hour_dict in day_dict.items():
                    for hour, frac in hour_dict.items():
                        month_conv = "20{:d}".format(int(month) + 10)
                        cur.execute(query, (frac, int(month_conv), day, hour, state))
    con.commit()
    con.close()


def get_elec_demand(
    startmonth, endmonth, months, year, demandfile="data/tva_electricity_demand.csv"
):
    """Reads and filters demand data for TEMOA

    Arguments:
        startmonth {str} -- Starting month of modeled period
        endmonth {str} -- Ending month of modeled period
        months {list} -- List of month three letter abbreviations
        year {str} -- Year of the starting month

    Keyword Arguments:
        demandfile {str} -- File name/location of demand data file (default: {"../data/tva_electricity_demand.csv"})

    Returns:
        dict -- Contains monthly demand indexed by period and state
    """
    nmonths = endmonth - startmonth + 1
    k = [2011 + i for i in range(int(nmonths))]
    if endmonth > 12:
        my_months = months[startmonth - 1 :]
        my_months += months[: endmonth - 12]
    else:
        my_months = months[startmonth - 1 : endmonth]

    # This all works fine for TVA, not great for any other scenario.
    # Moving to pandas may be a better idea for compatability
    output_demand = {
        period: {elc: "" for elc in ["AL", "GA", "KY", "MS", "NC", "TN", "VA"]}
        for period in k
    }
    demanddata = {
        str(i): {
            month: {elc: "" for elc in ["AL", "GA", "KY", "MS", "NC", "TN", "VA"]}
            for month in months
        }
        for i in range(2003, 2018)
    }

    with open(demandfile, "r") as f:
        for line in f:
            line = line.strip("\n\r")
            elc, yr, month, value = line.split(",")
            demanddata[yr][months[int(month) - 1]][elc] = float(value) / 1000

    for i, month in enumerate(my_months):
        value_list = demanddata[str(year)][month]
        for elc in ["AL", "GA", "KY", "MS", "NC", "TN", "VA"]:
            value = value_list[elc]
            name = f"ELC_{elc}"
            period = k[i]
            output_demand[period][elc] = (value, period, name)

    return output_demand


def modify_temoa_demand(inputFile, newDemand, nmonths):
    """Updates the demand requirments for TEMOA
    in the 'inputFile' database.

    Arguments:
        inputFile {str} -- Sqlite database filename
        newDemand {dict} -- Demand values indexed by period and by state
    """
    # state abbreviations for TVA
    states = ["AL", "GA", "KY", "MS", "NC", "TN", "VA"]

    con = sqlite3.connect(inputFile)  # connect to database
    cur = con.cursor()  # cursor to traverse tables
    query = """UPDATE Demand
			 SET demand = ?
			 WHERE periods = ? and demand_comm = ?;
			 """
    # * This should work for any number of months
    # * in temoa, months are indexed starting at 2011 regardless
    k = [2011 + i for i in range(int(nmonths))]
    for period in k:
        for state in states:
            try:
                cur.execute(query, newDemand[period][state])
            except sqlite3.OperationalError as e:
                print("Error when accessing:")
                print(inputFile)
                sys.exit()
    try:
        con.commit()  # commit changes to database
    except sqlite3.OperationalError as e:
        con.close()
        sys.exit()

def modify_temoa_config(file, db_file, scenario, solver="gurobi"):
    """Edits config file specified by 'file' to reflect
    current scenario, input and output files, and solver

    Arguments:
        file {str} -- Config file name/location
        db_file {str} -- database file for temoa's run
        scenario {str} -- scenario name for model run

    Keyword Arguments:
        solver {str} -- solver interface for pyomo to use (default: {'gurobi'})
    """
    #! Warning, the way temoa runs can be significantly altered by
    #! using this function incorrectly or bugs in the function.

    with open(file, "r") as f:
        data = f.read()
    # setup lines that will be inserted
    new_scen = "--scenario={}\n".format(scenario)
    solver_line = "--solver={}      # Optional, indicate the solver\n".format(solver)
    input_line = "--input={}\n".format(db_file)
    output_line = "--output={}\n".format(db_file)
    # substitution patterns
    sub_pats = [
        (r"--input=.*\n", input_line),
        (r"--output=.*\n", output_line),
        (r"--scenario=.*\n", new_scen),
        (r"--solver=.*\n", solver_line),
    ]
    # use regex to update config info
    for pat, repl in sub_pats:
        data = re.sub(pat, repl, data)
    # write data
    with open(file, "w") as f:
        f.write(data)


def update_initial_temoa_data(
    db_file,
    start_year,
    start_month,
    method,
    nmonths,
    months,
    rolling,
):
    file_prefix = get_prefix(start_month, int(nmonths))

    # update scenario names for identification of output
    new_scenario_name = f"{file_prefix}_{start_year}_{method}"

    if rolling:
        new_scenario_name += "_rolling"


    stop_month = start_month + int(nmonths) - 1
    
    new_demand = get_elec_demand(start_month, stop_month, months, start_year)

    modify_temoa_demand(db_file, new_demand, nmonths)
    modify_temoa_dsd(file_prefix, db_file)
    modify_temoa_capacity(db_file, new_scenario_name)
    return new_scenario_name


def get_reservoir_rules(start, stop):
    """Get the rule curves for reservoirs

    Args:
        start (int): start month
        stop (int): stop month

    Returns:
        tuple: two dictionaries containing lower and upper rule curves for reservoirs
    """
    # TODO : Fix all paths to be more generic
    df = pd.read_pickle("./data/reservoir_rule_curves.pickle")
    names = list({f'{i.split("_")[0]} Reservoir' for i in df.columns})
    lower = {name: [] for name in names}
    upper = {name: [] for name in names}
    for name in names:
        myname = name.split()[0]
        uname = f"{myname}_upper"
        lname = f"{myname}_lower"
        if stop < start:
            upper[name] = list(df[uname][start - 1 :].values)
            upper[name] += list(df[uname][:stop].values)
            lower[name] = list(df[lname][start - 1 :].values)
            lower[name] += list(df[lname][:stop].values)
        else:
            upper[name] = list(df[uname][start - 1 : stop].values)
            lower[name] = list(df[lname][start - 1 : stop].values)
    return (lower, upper)


def update_reservoir_rules(start, stop, input_path):
    lower, upper = get_reservoir_rules(start, stop)
    curve_pattern = re.compile(r"^0.1 +0.1")
    name_pattern = re.compile(r"\w+ Reservoir\n")
    edit_lines = []
    names = []
    # TODO : Fix all paths to be more generic
    with open(os.path.join(input_path, "reservoir_details.dat"), "r") as f:
        res_det = f.readlines()
    for i, line in enumerate(res_det):
        name_match = name_pattern.search(line)
        curve_match = curve_pattern.search(line)
        if name_match:
            names.append(line.strip("\n"))
        if curve_match:
            if len(edit_lines) == 0:
                edit_lines.append(i - 3)
            elif edit_lines[-1] == i - 1:  # if the last entry is the previous line
                continue
            else:
                edit_lines.append(i - 3)

    for i, name in enumerate(names):
        try:
            upper_pos = edit_lines[i]
        except IndexError as e:
            sys.exit()
        lower_pos = upper_pos + 1
        string = "{}\t" * (len(upper[name]) - 1) + "{}\n"
        upper_string = string.format(*upper[name])
        lower_string = string.format(*lower[name])
        res_det[upper_pos] = upper_string
        res_det[lower_pos] = lower_string
    with open(os.path.join(input_path, "reservoir_details.dat"), "w") as f:
        for line in res_det:
            f.write(line)


def update_initial_reservoir_storage(start_year, start_month, input_path):
    details_file = os.path.join(input_path, "reservoir_details.dat")

    with open(details_file, "r") as f:
        details = f.readlines()

    observed_data = pd.read_csv(
        "./data/tva_reservoir_data.csv"
    )
    observed_data["date"] = pd.to_datetime(observed_data["date"])
    observed_data = observed_data.set_index(["date", "reservoir"])
    obs_sto = observed_data["storage_1000_acft"].unstack()

    previous_day = find_previous_day(start_year, start_month, 1)
    init_storage = obs_sto.loc[previous_day]

    res_pat = re.compile(r"\w+ Reservoir")
    for i, line in enumerate(details):
        if re.search(res_pat, line):
            update_value = init_storage[line.split()[0]]
            update_line = details[i + 3].strip("\r\n").split()
            update_line[2] = str(update_value)
            update_line = "  ".join(update_line) + "\n"
            details[i + 3] = update_line

    with open(details_file, "w") as f:
        for line in details:
            f.write(line)


def update_initial_storage_for_rolling(
    scenario, input_path, start_month, nmonths
):
    prev_scen = change_scenario_for_rolling_window(
        scenario, start_month, nmonths, backwards=True
    )

    prev_scen_path = os.path.join(
        os.path.split(os.path.split(os.path.dirname(input_path))[0])[0],
        "graps_output",
        prev_scen,
    )

    if not os.path.isdir(prev_scen_path):
        if os.path.isdir(f"{prev_scen_path}_rolling"):
            prev_scen_path = f"{prev_scen_path}_rolling"
        else:
            print("There is not a previous run from which to use final values")
            raise FileNotFoundError

    storage_file = os.path.join(prev_scen_path, "storage.out")
    details_file = os.path.join(input_path, "reservoir_details.dat")

    with open(storage_file, "r") as f:
        storage = f.readlines()
    with open(details_file, "r") as f:
        details = f.readlines()

    final_storage = storage[-28:]
    res_pat = re.compile(r"\w+ Reservoir")

    update_values = {}
    for line in final_storage:
        line = line.strip("\n\r")
        line = line.split()
        name = " ".join(line[:2])
        new_value = line[2]
        update_values[name] = new_value

    for i, line in enumerate(details):
        if re.search(res_pat, line):
            update_value = update_values[line.strip("\n\r")]
            update_line = details[i + 3].strip("\r\n").split()
            update_line[2] = update_value
            update_line = "  ".join(update_line) + "\n"
            details[i + 3] = update_line

    with open(details_file, "w") as f:
        for line in details:
            f.write(line)


def update_reservoir_target_storage(input_path, stop, year):
    pattern = re.compile(r"(\d+\.\d+ +|\d+ +){5}(\d+\.\d+ *|\d+ *)")
    res_pat = re.compile(r"\w+ Reservoir")

    file = os.path.join(input_path, "reservoir_details.dat")
    with open(file, "r") as f:
        input_data = f.readlines()

    update_lines, names = [], []

    for i, line in enumerate(input_data):
        if re.search(res_pat, line):
            names.append(line.strip("\n"))
        if re.search(pattern, line):
            update_lines.append([i, line])

    observed_data = pd.read_csv(
        "./data/tva_reservoir_data.csv"
    )
    observed_data["date"] = pd.to_datetime(observed_data["date"])
    observed_data = observed_data.set_index(["date", "reservoir"])
    obs_sto = observed_data["storage_1000_acft"].unstack()

    new_targets = {}
    last_day_month = calendar.monthrange(int(year), int(stop))[1]
    target_day = datetime(int(year), int(stop), last_day_month)

    # load targets from observed data
    for name in names:
        target = obs_sto.loc[target_day, name.split()[0]] 
        new_targets[name] = target

    # rewrite targets in reservoir_details data
    for name, line in zip(names, update_lines):
        line_num, line_vals = line
        values = line_vals.split()
        values[4] = str(round(new_targets[name], 3))
        new_line = "  ".join(values) + "\n"
        input_data[line_num] = new_line
    
    # write reservoir_details file with updated targets
    with open(file, "w") as f:
        for line in input_data:
            f.write(line)


def update_max_release(input_path):
    user_file = os.path.join(input_path, "user_details.dat")
    # pattern = re.compile(r"^\D+ H")
    pattern = re.compile(r"^\D+$|^\D+\d{1} H$")
    df = pd.read_pickle("./data/max_release.pickle")
    with open(user_file, "r") as f:
        user_data = f.readlines()
    for i, line in enumerate(user_data):
        if re.search(pattern, line):
            my_line = user_data[i + 4]
            my_line = my_line.split()
            name = line.strip("\n\r")
            if name == "Watuga H":
                name = "Watauga H"
            new_value = str(df[name])
            my_line[-2] = new_value
            my_line = "\t".join(my_line) + "\n"
            user_data[i + 4] = my_line
    with open(user_file, "w") as f:
        for line in user_data:
            f.write(line)


def update_graps_opt_params(input_path):
    nf = "1"  # Number of objective functions
    mode = "210"  # CBA - reference FFSQP Manual for meaning
    iprint = "1"  # Controls level of output for FFSQP
    miter = "1000"  # Max iterations
    bigbnd = "1.d+10"  # acts as infinite bound on decision vars
    eps = "1.d-4"  # convergence criterion - smaller the number the longer the model takes to converge
    epseqn = "0.01"  # tolerance for constraints, will end up being the machine precision epsmac in FFSQP
    udelta = (
        "0"  # perturbation size, reference FFSQP manual on how this is actually used
    )

    values = [nf, mode, iprint, miter, bigbnd, eps, epseqn, udelta]
    file_name = "model_para.dat"
    with open(os.path.join(input_path, file_name), "w") as f:
        for value in values:
            f.write(f"{value}\n")


def update_graps_hydro_capacity(input_path, scenario):
    exist_cap_file = "data/existing_capacity.csv"
    df = pd.read_csv(exist_cap_file)
    if scenario[:3] == "DJF":
        key = "winter"
    elif scenario[:3] == "JAS":
        key = "summer"
    else:
        key = ["winter", "summer"]
    temoa_names = {
        "Apalachia H": "Apalachia_HY_TN",
        "BlueRidge H": "BlueRidge_HY_GA",
        "Boone H": "Boone_HY_TN",
        "Chatuge H": "Chatuge_HY_NC",
        "Cherokee H": "Cherokee_HY_TN",
        "Chickamauga H": "Chickamauga_HY_TN",
        "Douglas H": "Douglas_HY_TN",
        "Fontana H": "Fontana_HY_NC",
        "FortLoudoun H": "FortLoudoun_HY_TN",
        "FtPatrick H": "FortPatrick_HY_TN",
        "Guntersville H": "Guntersville_HY_AL",
        "Hiwassee H": "Hiwassee_HY_NC",
        "Kentucky H": "Kentucky_HY_KY",
        "MeltonH H": "MeltonHill_HY_TN",
        "Nikajack H": "Nickajack_HY_TN",
        "Norris H": "Norris_HY_TN",
        "Nottely H": "Nottely_HY_GA",
        "Ocoee1 H": "Ocoee1_HY_TN",
        "Ocoee3 H": "Ocoee3_HY_TN",
        "Pickwick H": "PickwickLanding_HY_TN",
        "RacoonMt H": "RaccoonMt_Storage_TN",
        "SHolston H": "SouthHolston_HY_TN",
        "TimsFord H": "TimsFord_HY_TN",
        "WattsBar H": "WattsBar_HY_TN",
        "Watuga H": "Watauga_HY_TN",
        "Wheeler H": "Wheeler_HY_AL",
        "Wilbur H": "Wilbur_HY_TN",
        "Wilson H": "Wilson_HY_AL",
    }
    update_nums = {
        "Watuga H": 9,
        "Wilbur H": 22,
        "SHolston H": 35,
        "Boone H": 48,
        "FtPatrick H": 61,
        "Cherokee H": 74,
        "Douglas H": 87,
        "FortLoudoun H": 100,
        "Fontana H": 113,
        "Norris H": 126,
        "MeltonH H": 139,
        "WattsBar H": 152,
        "Chatuge H": 165,
        "Nottely H": 178,
        "Hiwassee H": 191,
        "Apalachia H": 204,
        "BlueRidge H": 217,
        "Ocoee3 H": 230,
        "Ocoee1 H": 243,
        "Chickamauga H": 256,
        "RacoonMt H": 269,
        "Nikajack H": 282,
        "Guntersville H": 295,
        "TimsFord H": 308,
        "Wheeler H": 321,
        "Wilson H": 334,
        "Pickwick H": 347,
        "Kentucky H": 360,
    }
    details_file = os.path.join(input_path, "user_details.dat")
    with open(details_file, "r") as f:
        data = list(f.readlines())

    for name, temoa_name in temoa_names.items():
        num = update_nums[name]
        row = df[df["tech"] == temoa_name]
        line = data[num]
        cap = row[key].mean().values[0] if type(key) == list else row[key].values[0]
        line_split = line.split()
        line_split[1] = str(cap)
        new_line = "  ".join(line_split) + "\n"
        data[num] = new_line

    with open(details_file, "w") as f:
        f.writelines(data)


def update_reservoir_inflow_data(start_year, start_month, nmonths, input_path):
    inflow_files = glob.glob(os.path.join(input_path, "InflowData/*"))

    observed_data = pd.read_csv(
        "./data/tva_reservoir_data.csv"
    )
    observed_data["date"] = pd.to_datetime(observed_data["date"])
    observed_data = observed_data.set_index(["date", "reservoir"])
    inflow = observed_data["uncontrolled_inflow_cfs"].unstack()

    initial_date = datetime(start_year, start_month, 1)
    stop_year = start_year
    stop_month = int(start_month) + int(nmonths)
    if stop_month > 12:
        stop_month -= 12
        stop_year += 1
    end_date = datetime(stop_year, stop_month, 1)

    inflow = inflow.loc[pd.date_range(initial_date, end_date, closed="left")]
    inflow *= 3600 * 24 / 43560 / 1000 # cfs to 1000 acre-ft / day
    inflow = inflow.resample("MS").sum().T

    for file in inflow_files:
        fname = pathlib.Path(file).name
        res = fname.split("_")[0]
        values = inflow.loc[res].values
        with open(file, "w") as f:
            f.write("\t".join(map(str, values)))
    

def rewrite_rcmt_inflow(input_path, ntime):
    file_path = pathlib.Path(input_path) / "InflowData" / "RacoonMt_inflow.dat"

    with open(file_path.as_posix(), "w") as f:
        f.write("\t".join("0.0" for i in range(int(ntime))))


def add_rcmt_inflow_to_decvars(input_path, start_year, start_month, nmonths):
    df = pd.read_pickle("./data/rcmt_flow_info.pickle")
    file_path = pathlib.Path(input_path) / "decisionvar_details.dat"
    index = (
        (df.index.year == int(start_year)) & (df.index.month == int(start_month))
    ).tolist()
    start_index = index.index(True)
    stop_index = start_index + int(nmonths)
    canal = df.loc[df.index[start_index:stop_index], "Canal"]
    with open(file_path.as_posix(), "a") as f:
        f.write("\n".join(f"{i:.3f}" for i in canal.values) + "\n")


def update_graps_input_files(
    start_year,
    start_month,
    nmonths,
    input_path,
    scenario,
    rolling,
    first,
):  # sourcery no-metrics
    """Uses shells scripts, R scripts, and directory manipulation 
    to make reservoir input files with correct data for the modeled
    time period

    Arguments:
        start_year {str} -- Starting year
        start_month {str} -- Starting month

    """
    start_year = int(start_year)
    start_month = int(start_month)
    end_month = start_month + int(nmonths) - 1
    end_year = start_year
    while end_month > 12:
        end_month -= 12
        end_year += 1

    # start_string = format_string.format(year=start_year, month=start_month, day=1)
    # end_day = get_last_day(end_year, end_month)
    # end_string = format_string.format(year=end_year, month=end_month, day=end_day)

    # for file in glob.glob("graps_input/AMJ_04/*"):
    #     if not os.path.isdir(file):
    #         shutil.copy(file, input_path)

    # curr_cwd = os.getcwd()
    # os.chdir("ReservoirModel_preparation/")
    # cmd_output_1 = subprocess.check_output(
    #     ["Rscript", "./make_outputs.R", start_string, end_string]
    # )
    # cmd_output_2 = subprocess.check_output(
    #     [
    #         "./running.sh",
    #         nmonths,
    #         input_path[:-1],
    #         monthf.format(start_month),
    #         monthf.format(end_month),
    #         "0",
    #     ]
    # )
    # os.chdir(curr_cwd)
    update_reservoir_rules(start_month, end_month, input_path)
    update_initial_reservoir_storage(start_year, start_month, input_path)
    update_reservoir_inflow_data(start_year, start_month, nmonths, input_path)
    # rewrite_rcmt_inflow(input_path, nmonths)
    # add_rcmt_inflow_to_decvars(input_path, start_year, start_month, nmonths)

    update_graps_opt_params(input_path)
    if rolling and not first:
        update_initial_storage_for_rolling(
            scenario, input_path, start_month, nmonths
        )

    update_max_release(input_path)
    update_reservoir_target_storage(input_path, end_month, start_year)
    update_graps_hydro_capacity(input_path, scenario)


def get_data_from_database(filename, scenario, db_file):
    """Used to parse data from database 'filename'
    for scenario 'scenario'

    Arguments:
        filename {str} -- File name/location for database
        scenario {str} -- Scenario to get data for
    """
    con = sqlite3.connect(db_file)
    cur = con.cursor()  # A database cursor enables traversal over DB records
    con.text_factory = str  # This ensures data is explored with UTF-8 encoding

    generators = {
        row[0]
        for row in con.execute("SELECT * FROM technologies;")
        if row[1] in ["p", "pb", "ps"] and row[0][:2] != "TD"
    }

    sql = (
        "SELECT t_periods, tech, scenario, sum(vflow_out) FROM Output_VFlow_Out WHERE tech IN {} and scenario = '"
        + scenario
        + "' GROUP BY t_periods, tech;"
    )
    sql = sql.format(tuple(generators))

    data = cur.execute(sql)
    with open(filename + ".csv", "w") as f:
        writer = csv.writer(f)
        writer.writerow(["Month", "Technology", "Scenario", "MWh"])
        writer.writerows(data)
    con.close()


def clear_reservoir_files(scenario_name, output_dir):
    """Clears reservoir files of data from the last run.
    # TODO : This should be used to clear the output files
    Arguments:
        scenario_name {str} -- Scenario name used for directory where files are stored
    """
    cur_dir = os.getcwd()
    if not os.path.exists(output_dir):
        os.mkdir(output_dir)
    else:
        os.chdir(output_dir)
        for file in os.listdir(os.getcwd()):
            with open(file, "w") as f:
                f.write("")
    os.chdir(cur_dir)


def change_scenario_for_rolling_window(scenario, start_month, nmonths, backwards=False):
    """Updates the scenario by moving to the next month
    for rolling window analysis

    Arguments:
        scenario {str} -- string containing modeled months and year
        backwards {bool} (default = False) -- indicates if getting previous scenario

    Returns:
        str -- string for scenario for next month
    """
    split_scenario = scenario.split("_")

    if len(split_scenario) == 4:
        prefix, year, method, rolling = split_scenario
        roll_flag = True
    else:    
        prefix, year, method = split_scenario
        roll_flag = False

    new_year = int(year)
    if backwards:
        new_start = int(start_month) - 1
        if new_start < 1:
            new_start += 12
            new_year -= 1
    else:
        new_start = int(start_month) + 1
        if new_start > 12:
            new_start -= 12
            new_year += 1

    new_prefix = get_prefix(new_start, int(nmonths))
    if roll_flag:
        return "_".join([new_prefix, str(new_year), method, rolling])
    else:
        return "_".join([new_prefix, str(new_year), method])

class COREGS(object):
    """
    This model will utilize an iterative algorithm to optimize 
    a connected energy and reservoir system by increasing hydropower
    production to reduce total cost of meeting electricity demand 
    while maintaining reservoir constraints
    """

    def __init__(
        self,
        args,
        SO=None,
        persistent="N",
        start_year=None,
        start_month=None,
        nmonths=None,
        param_num=None,
    ):
        """
        initialize the object and setup the modeling environment 

        Arguments:
            args {dict} -- arguments parsed from command line

        Keyword Arguments:
            SO {_io.TextIOWrapper} -- allows users to redirect stdout (default: {None})
            persistent {str} -- allows a persistant solver to be used (default: {'N'})
            start_year {str} -- model start year (default: {None})
            start_month {str} -- model start month (default: {None})
            param_num {int} -- number of parameters for GRAPS (default: {None})
        """

        for key, value in args.items():
            setattr(self, key, value)

        self.months = list(calendar.month_abbr)[1:]
        self.nmonths = "3"

        if SO is None:
            SO = sys.stdout

        if self.stdout:
            devnull = open(os.devnull, "w")
            SO = devnull

        self.SO = SO

        # getting args and setting up variables
        if start_year is None:
            self.year = self.start_year
            self.full_year = True
            if self.start_month != "year":
                self.start_month = int(self.start_month)
                self.full_year = False
            self.n_params = int(self.n_init_params)
        else:
            self.year = start_year
            self.full_year = True
            if start_month is None:
                start_month = "year"
            else:
                start_month = int(start_month)
                self.full_year = False
            self.n_params = int(param_num)

        # updating initial data in sqlite database and config file
        if persistent == "N":
            solver = "gurobi"
        elif persistent == "Y":
            solver = "gurobi_persistent"

        self.db_file = "data/tva_temoa.sqlite"
        self.set_all_capacities()

        self.scen_name = update_initial_temoa_data(
            self.db_file,
            self.start_year,
            self.start_month,
            self.method,
            self.nmonths,
            self.months,
            self.rolling,
        )

        self.find_in_out_paths()
        self.check_in_out_path_exist()
        self.config_file = os.path.join(self.input_path, "temoa_config")
        # if not os.path.exists(self.config_file):
        #     shutil.copy("temoa_config", self.input_path)

        modify_temoa_config(self.config_file, self.db_file, self.scen_name, solver)
        self.log_file = open(
            os.path.join(self.output_path, self.scen_name + ".log"), "w"
        )
        update_graps_input_files(
            self.start_year,
            self.start_month,
            self.nmonths,
            self.input_path,
            self.scen_name,
            self.rolling,
            self.first,
        )
        self.change_sholston_details()
        self.find_upstream_reservoirs()
        # self.get_target_storage()

        self.end_month = self.start_month + int(self.nmonths)
        self.end_year = int(self.start_year)
        if self.end_month > 12:
            self.end_month -= 12
            self.end_year += 1
            self.end_year = str(self.end_year)
        self.t_start = "{}-{}".format(self.start_year, self.start_month)
        self.t_stop = "{}-{}".format(self.end_year, self.end_month)
        # clear duals.dat
        with open(os.path.join(self.output_path, "duals.dat"), "w+") as f:
            pass
        self.clear_objective_file()

    def write(self, string):
        self.log_file.write(string)
        self.SO.write(string)
        self.SO.flush()

    def change_sholston_details(self):
        new_min = 326.0
        details_file = "reservoir_details.dat"
        with open(os.path.join(self.input_path, details_file), "r") as f:
            details = f.readlines()
        line = details[44]
        line = line.split()
        line[1] = str(new_min)
        replace = "   ".join(line) + "\n"
        details[44] = replace
        with open(os.path.join(self.input_path, details_file), "w") as f:
            f.writelines(details)

    def initialize(self):
        """
        initialize and run reservoir model to provide base line hydropower values for 
        temoa. Instantiate temoa with value provided from reservoir model.
        """
        start = timer()
        # clearing reservoir files

        clear_reservoir_files(self.scen_name, self.output_path)
        # instantiating models
        self.create_solver_instance_temoa()
        self.res_model = GRAPS(
            self.n_params, self.input_path, self.output_path, self.method
        )
        self.res_model.initialize_model()

        # initial reservoir model run
        self.res_model.simulate_model("run1")
        # getting new maxactivity for temoa
        self.res_model.create_new_max_act(int(self.nmonths))
        # populate temoa_model with data
        self.temoa_model = self.create_instance_temoa()

        # need the next line for using persistent solver
        # self.temoa_instance.optimizer.set_instance(self.temoa_model)

        self.temoa_model.dual = Suffix(direction=Suffix.IMPORT_EXPORT)
        self.temoa_model.rc = Suffix(direction=Suffix.IMPORT)
        self.objective = getattr(self.temoa_model, "TotalCost")

        # update temoa activity with new activity for hydropower
        self.change_activity()
        stop = timer()
        self.write("\n\tSetup time: {:0.3f} seconds\n".format(stop - start))


    def check_in_out_path_exist(self):
        if not os.path.isdir(self.input_path):
            shutil.copytree(
                os.path.join(os.path.split(self.input_path[:-1])[0], "default"),
                self.input_path,
            )
        if not os.path.isdir(self.output_path):
            os.mkdir(self.output_path)


    def run_FFSQP(self):
        clear_reservoir_files(self.scen_name, self.output_path)

        self.create_solver_instance_temoa()
        self.res_model = GRAPS(
            self.n_params, self.input_path, self.output_path, self.method
        )

        self.res_model.initialize_model()
        self.res_model.simulate_model("run1")
        self.create_mass_balance_output()
        self.res_model.create_new_max_act(int(self.nmonths))
        self.temoa_model = self.create_instance_temoa()

        self.temoa_model.dual = Suffix(direction=Suffix.IMPORT_EXPORT)
        self.temoa_model.rc = Suffix(direction=Suffix.IMPORT)
        self.objective = getattr(self.temoa_model, "TotalCost")

        self.change_activity()
        self.solve_temoa()
        self.write_objective_value(0)
        duals = self.get_activity_duals()
        self.write_duals(duals, 1, self.output_path)
        self.get_hydro_benefits()
        self.res_model.optimize_model("run2")

        self.res_model.create_new_max_act(int(self.nmonths))
        self.change_activity()
        self.solve_temoa()
        self.write_objective_value(1)
        duals = self.get_activity_duals()
        self.write_duals(duals, 2, self.output_path)
        self.temoa_model.solutions.store_to(self.temoa_instance.result)
        formatted_results = pformat_results.pformat_results(
            self.temoa_model, self.temoa_instance.result, self.temoa_instance.options
        )
        output_file = "./generation_output/" + self.scen_name
        get_data_from_database(output_file, self.scen_name, self.db_file)

        self.create_mass_balance_output()
        # self.moveTempDB()

    def find_in_out_paths(self):
        cur_dir = os.getcwd()
        self.input_path = os.path.join(cur_dir, "graps_input", self.scen_name + "/")
        self.output_path = os.path.join(cur_dir, "graps_output", self.scen_name + "/")


    def get_activity_duals(self):
        """Get dual variables for MaxActivityConstraint within Temoa.

        Returns:
            dict -- Contains dual variables with index similar to that in temoa. 
        """
        cons = getattr(self.temoa_model, "MaxActivityConstraint")
        return {index: self.temoa_model.dual.get(cons[index]) for index in cons}

    def get_hydro_benefits(self):
        duals = self.get_activity_duals()
        ntime = int(self.nmonths)
        k = [2011 + i for i in range(ntime)]
        duals_w_index = {}
        for i, month in enumerate(k):
            for res_id in range(1, self.res_model.nparam // ntime + 1):
                # get the name of the reservoir in temoa using the reservoir id from GRAPS
                temoa_name = self.res_model.temoa_names.get(res_id, None)
                if temoa_name:
                    # setup the index temoa uses
                    index = (month, temoa_name)
                    # pull the correct dual for the index
                    dual = duals[index]
                    # setup numerical index for addressing reservoir decision variables
                    num_index = (res_id - 1) * ntime + i
                    # store the dual variable with the proper indexing
                    duals_w_index[index] = [num_index, res_id, dual]

        for num_index, res_id, dual in duals_w_index.values():
            self.res_model.py_hydro_benefit[num_index] = abs(dual) / 1000

    def write_duals(self, duals, iteration, output_path):
        with open(os.path.join(self.output_path, "duals.dat"), "a+") as f:
            for key, value in list(duals.items()):
                f.write("{},{},{}\n".format(iteration, key, value))

    def change_decision_vars(self, iteration, alpha):
        """Heart of the iterative process.
        Updates decision variables (releases) for reservoir model based on
        dual variable (shadow prices) in temoa for hydropower output and 
        physical and operational contraints in the reservoir model.

        Arguments:
            iteration {int} -- Iteration number
        """
        # get dual variables for Hydropower constraint
        duals = self.get_activity_duals()
        if iteration == 1:
            self.initial_duals = duals

        # write the dual variables
        self.write_duals(duals, iteration, self.output_path)
        # get reservoir model decision variables
        dec_vars = self.res_model.dec_vars
        # get hydro benefit
        hydro_benefit = self.res_model.hydro_benefit

        # determine proper indexing
        spill = self.res_model.spill_dict
        deficit = self.res_model.deficit_dict

        ntime = int(self.nmonths)
        k = [2011 + i for i in range(ntime)]

        duals_w_index = {}

        for i, month in enumerate(k):
            for res_id in range(1, self.res_model.nparam // ntime + 1):
                # get the name of the reservoir in temoa using the reservoir id from GRAPS
                temoa_name = self.res_model.temoa_names.get(res_id, None)
                # check if the graps node exists in temoa
                if temoa_name != None:
                    # setup the index temoa uses
                    index = (month, temoa_name)
                    # pull the correct dual for the index
                    dual = duals[index]
                    # setup numerical index for addressing reservoir decision variables
                    num_index = (res_id - 1) * ntime + i
                    # store the dual variable with the proper indexing
                    duals_w_index[index] = [num_index, res_id, dual]

        # sort the dual variables in ascending order
        sorted_duals = sorted(list(duals_w_index.items()), key=lambda x: x[1][2])
        max_dual = abs(sorted_duals[0][1][2])
        max_dual2 = abs(sorted_duals[1][1][2])

        # Main decision loop,
        # fix spill and deficit
        decreased_num_indices = []
        for index, (num_index, res_id, dual) in sorted_duals:
            dec_vars, decreased_num_indices_iter = self.fix_spill_and_deficit(
                num_index, res_id, spill, deficit, ntime, dec_vars
            )
            for j in decreased_num_indices_iter:
                decreased_num_indices.append(j)

        for index, (num_index, res_id, dual) in sorted_duals:
            if num_index in decreased_num_indices:
                # if the decision variable was decreased to fix spill, continue to next one
                continue

            dec_var = dec_vars[num_index]
            storage_difference = self.res_model.res_violations[res_id]

            increase_ratio = abs(dual) / (max_dual * alpha)

            # do not want to check floats for == 0
            if dec_var < 0 + 0.001:
                if increase_ratio > 0:
                    dec_var = 0.05 * self.res_model.release_bounds[res_id][1]
            else:
                dec_var += increase_ratio * dec_var

            # if the new decision variable results in a violation of release bounds, modify it to be at the bound.
            if dec_var > self.res_model.release_bounds[res_id][1]:
                dec_var = self.res_model.release_bounds[res_id][1]
            elif dec_var < self.res_model.release_bounds[res_id][0]:
                dec_var = self.res_model.release_bounds[res_id][0]

            # reassign variable
            dec_vars[num_index] = dec_var
            hydro_benefit[num_index] = abs(dual)

            # check for racoon mt storage
            _m, temoa_name = index
            if temoa_name == "RaccoonMt_Storage_TN":
                # not really a fan of this
                # logically, over the course of a month, the inflow to racoon mt
                # and the outflow should be approximately equal.
                # So I am just going to set them to be equal for now.
                pump_station_id = 29
                ps_num_index = (pump_station_id - 1) * ntime + (num_index - 1) % ntime
                dec_vars[ps_num_index] = dec_vars[num_index]

        self.res_model.dec_vars = dec_vars
        self.res_model.hydro_benefit = hydro_benefit

    def fix_spill_and_deficit(self, num_index, res_id, spill, deficit, ntime, dec_vars):
        decreased_num_indices = []
        my_spill = spill[res_id][num_index % ntime]
        my_deficit = deficit[res_id][num_index % ntime]
        upstream_res = self.res_parents[int(res_id)]
        # if it is slighlty more than zero,
        # allow numbers smaller than 1e-8 due to FP precision
        if my_spill > 1e-8:
            total_uprelease = 0
            for ptype, pid in upstream_res:
                uprelease_index = (pid - 1) * ntime + num_index % ntime
                total_uprelease += dec_vars[uprelease_index]
            if total_uprelease <= 0:
                total_uprelease = 1
            total_decrease = 0
            for ptype, pid in upstream_res:
                uprelease_index = (pid - 1) * ntime + num_index % ntime
                uprelease = dec_vars[uprelease_index]
                fraction = uprelease / total_uprelease
                dec_vars[uprelease_index] -= my_spill * fraction
                total_decrease += my_spill * fraction
                decreased_num_indices.append(uprelease_index)

        if my_deficit > 1e-8:
            total_uprelease = 0
            for ptype, pid in upstream_res:
                uprelease_index = (pid - 1) * ntime + num_index % ntime
                total_uprelease += dec_vars[uprelease_index]
            if total_uprelease <= 0:
                total_uprelease = 1
            # I want the sum of the remaining fractions
            # since rem fraction = (T - U_r)/T for all r in R
            # and the sum of U_r for all r in R = T
            # the rem fraction sum becomes
            # sum (r in R) (T-U_r)/T = Div.
            # sum (r in R) (T-U_r) = T x Div.
            # N_R x T - T = T x Div
            # N_R - 1 = Div.
            # so we can set the divisor to be 1 less than the # of upstream res
            # this avoids a second loop
            divisor = len(upstream_res) - 1
            for ptype, pid in upstream_res:
                uprelease_index = (pid - 1) * ntime + num_index % ntime
                uprelease = dec_vars[uprelease_index]
                # this gives up a number that represents how much the other reservoirs contribute
                # to the total inflow of this reservoir
                rem_fraction = (total_uprelease - uprelease) / total_uprelease
                fraction = 1 if divisor <= 0 else rem_fraction / divisor

                dec_vars[uprelease_index] += my_deficit * fraction
        return dec_vars, decreased_num_indices

    def find_upstream_reservoirs(self):
        respath = os.path.join(self.input_path, "reservoir_details.dat")
        nodepath = os.path.join(self.input_path, "node_details.dat")
        res_pattern = re.compile("^\D+ Reservoir")
        node_pattern = re.compile("Junction")

        with open(respath, "r") as f:
            res_data = f.readlines()

        with open(nodepath, "r") as f:
            node_data = f.readlines()

        res_parent_data = defaultdict(list)
        node_parent_data = defaultdict(list)
        for i, line in enumerate(res_data):
            if re.search(res_pattern, line):
                inum = int(res_data[i - 1].strip("\n"))
                spill_line = i + 6
                parent_line = i + 8
                nspill, noutlet = res_data[spill_line].split()
                nchild, nparent = res_data[parent_line].split()
                nparent = int(nparent)
                parent_start_line = i + 9 + int(nspill) + int(nchild)
                parent_end = parent_start_line + nparent
                for j in range(nparent):
                    parent = res_data[parent_start_line + j]
                    ptype, pid = parent.split()
                    if ptype in ["3", "5"]:
                        res_parent_data[inum].append((int(ptype), int(pid)))

        for i, line in enumerate(node_data):
            if re.search(node_pattern, line):
                inum, nchild, nparent = node_data[i + 1].split()
                nparent = int(nparent)
                parent_start_line = i + 2 + int(nchild)
                parent_end = parent_start_line + nparent
                for j in range(nparent):
                    parent = node_data[parent_start_line + j]
                    ptype, pid = parent.split()
                    if ptype in ["3", "5"]:
                        node_parent_data[int(inum)].append((int(ptype), int(pid)))

        for rid, parents in res_parent_data.items():
            for i, (ptype, pid) in enumerate(parents):
                if ptype == 5:
                    new_parents = node_parent_data[pid]
                    res_parent_data[rid].pop(i)
                    for new_p in new_parents:
                        res_parent_data[rid].append(new_p)
        self.res_parents = res_parent_data

    def single_run(self):
        """
        Runs the model just once to get baseline values
        """
        self.solver.options.scenario = self.solver.options.scenario + "_single"
        self.scen_name = self.solver.options.scenario
        self.solve_temoa()
        self.temoa_model.solutions.store_to(self.temoa_instance.result)
        formatted_results = pformat_results.pformat_results(
            self.temoa_model, self.temoa_instance.result, self.temoa_instance.options
        )
        output_file = "./generation_output/" + self.scen_name 
        get_data_from_database(output_file, self.scen_name, self.db_file)
        self.violation_count = False
        return None

    def icorps(self, epsilon=None):
        """Iterative portion of the algorithm.
        Continues to try to improve the solution until 
        the change in objective function is less than an epsilon value
        or until the iteration has exceeded the max_iter. 

        Keyword Arguments:
            max_iter {int} -- Maximum iteration number (default: {10})
            epsilon {float} -- Change in objective function threshold (default: {None})

        Returns:
            int -- Iteration number
        """
        # helper functions for determining reservoir violations
        def count_spill_and_deficit(spill, deficit):
            count_spill = 0
            count_def = 0
            for values in spill.values():
                for item in values:
                    if item > 1e-8:
                        count_spill += 1
            for values in deficit.values():
                for item in values:
                    if item > 1e-8:
                        count_def += 1
            return count_spill + count_def

        alpha = self.alpha
        self.write(f"Solving model with epsilon = {epsilon} and alpha = {alpha}\n")

        self.solve_temoa()  # solve temoa to get an initial solution

        # list for storing costs after each iteration
        # starts with very large number to ensure that the algorithms
        # loop starts
        try:
            self.recorded_costs = [float(10 ** 20), self.get_objective_value()]
        except ValueError as e:
            II()
            sys.exit()

        self.last_cost = self.recorded_costs[1]  # most recent cost

        self.write(
            "Initial Objective Value (TotalCost): ${:,.2f}\n".format(self.last_cost)
        )
        self.write_objective_value(0)

        iteration = 1  # iteration counter
        self.violation_count = defaultdict(int)
        self.last_cons = set()

        def check_change_percent(new, old, eps=epsilon):
            return (float(old) - float(new)) / float(old) >= eps

        # control the convergence criteria with this variable
        # convergence_num : number of times the model objective
        # change has to be less than epsilon before the model will converged
        convergence_num = 5
        num_no_change = 0

        while num_no_change < convergence_num:
            # change reservoir decision variables
            self.last_cons = set()
            for res_id, value in list(self.res_model.res_constraints.items()):
                if value > 1e-8:
                    self.last_cons.add(res_id)
            for res_id, value in list(self.res_model.res_violations.items()):
                if value < -1e-8:
                    name = self.res_model.res_names[res_id]
                    self.violation_count[name] += 1

            self.change_decision_vars(iteration, alpha)
            # simulate the reservoir system again with new decision variables
            self.res_model.simulate_model("iter_" + str(iteration))
            # create the new max activity bounds for temoa
            self.res_model.create_new_max_act(int(self.nmonths))
            # change temoa hydropower max activity bounds
            self.change_activity()
            self.write("\n")
            # solve temoa
            self.solve_temoa()
            new_cost = self.get_objective_value()

            self.write("Objective Value (TotalCost): ${:,.2f}\n".format(new_cost))
            self.write(
                Fore.GREEN
                + Style.BRIGHT
                + "Objective Value Change: {:.2f}%\n".format(
                    (self.recorded_costs[iteration] - new_cost)
                    / self.recorded_costs[iteration]
                    * 100
                )
                + Style.RESET_ALL
            )

            if not check_change_percent(
                self.recorded_costs[iteration], self.recorded_costs[iteration - 1]
            ):
                num_no_change += 1

            self.write_objective_value(iteration)
            self.recorded_costs.append(new_cost)
            self.last_cost = new_cost
            iteration += 1

        num_spill_def = count_spill_and_deficit(
            self.res_model.spill_dict, self.res_model.deficit_dict
        )

        self.write("Meeting Reservoir Constraints\n")
        self.write(f"Inital Spill and Def Num: {num_spill_def}\n")
        
        if num_spill_def > 0:
            for num_index in range(self.n_params):
                ntime = int(self.nmonths)
                res_id = num_index // ntime + 1
                if res_id <= 28:
                    dec_vars, decreased_num_indices_iter = self.fix_spill_and_deficit(
                        num_index,
                        res_id,
                        self.res_model.spill_dict,
                        self.res_model.deficit_dict,
                        ntime,
                        self.res_model.dec_vars,
                    )
                    self.res_model.simulate_model("fixing_spill_deficit")
                    num_spill_def = count_spill_and_deficit(
                        self.res_model.spill_dict, self.res_model.deficit_dict
                    )

        self.write(f"Final Spill and Def Num: {num_spill_def}\n")

        def write_spill_and_deficit(spill_dict, deficit_dict):
            self.write("Spilling Reservoirs\n")
            for key, item in spill_dict.items():
                if any(item):
                    name = self.res_model.res_names[key]
                    self.write(f"\t{name}: {item}\n")
            self.write("Deficit Reservoirs\n")
            for key, item in deficit_dict.items():
                if any(item):
                    name = self.res_model.res_names[key]
                    self.write(f"\t{name}: {item}\n")

        write_spill_and_deficit(self.res_model.spill_dict, self.res_model.deficit_dict)

        self.res_model.create_new_max_act(int(self.nmonths))

        self.change_activity()
        self.solve_temoa()
        new_cost = self.get_objective_value()
        self.write("Objective Value (TotalCost): ${:,.2f}\n".format(new_cost))

        self.temoa_model.solutions.store_to(self.temoa_instance.result)
        formatted_results = pformat_results.pformat_results(
            self.temoa_model, self.temoa_instance.result, self.temoa_instance.options
        )
        output_file = "./generation_output/" + self.scen_name 
        get_data_from_database(output_file, self.scen_name, self.db_file)
        self.first_cost = self.recorded_costs[1]
        self.last_cost = new_cost
        self.write_objective_value(iteration + 1)
        self.log_file.close()

        self.create_mass_balance_output()
        if self.stdout:
            self.SO.close()
        return iteration

    def set_all_capacities(self):
        """
        Set all capacities of technologies to their baseline numbers
        """
        df = pd.read_csv("data/existing_cap_old.csv")
        con = sqlite3.connect(self.db_file)
        cur = con.cursor()
        sql = "UPDATE ExistingCapacity SET exist_cap={} WHERE tech ='{}';"
        for i, row in df.iterrows():
            cur.execute(sql.format(row["NEW"], row["tech"]))
        con.commit()
        con.close()

    def create_solver_instance_temoa(self):
        """
        Creates a temoa solver instance
        """
        self.T_model = temoa.model
        self.solver = temoa_run.TemoaSolver(
            self.T_model, os.path.abspath(self.config_file)
        )

        # initialize temoa_instance object
        self.temoa_instance = temoa_run.TemoaSolverInstance(
            self.T_model,
            self.solver.optimizer,
            self.solver.options,
            open(os.path.abspath("coregs_run.log"), "w"),
        )

    def create_instance_temoa(self):
        """Instantiates temoa with data

        Returns:
            pyomo.instance -- Temoa instantiated model
        """
        # temoa_instance.create_temoa_instance() returns an iterator.
        # to create self.instance must use it in an iterative manner.
        for _ in self.temoa_instance.create_temoa_instance():
            pass
        return self.temoa_instance.instance

    def solve_temoa(self):
        """
        Solves temoa model. Used in the iterative loop.
        """
        for _ in self.temoa_instance.solve_temoa_instance():
            pass
        return None

    def get_objective_value(self):
        return self.objective()

    def change_activity(self):
        """
        Changes temoa activity bounds for hydropower with results from reservoir model       
        """
        for index in self.res_model.new_max_act:
            try:
                # to GWh/month
                self.temoa_model.MaxActivity[index].value = (
                    self.res_model.new_max_act[index] // 1000
                )
            except KeyError as e:
                self.write(e + "\n")
                sys.exit("KeyError in change_activity()")
        self.temoa_model.MaxActivityConstraint.reconstruct()

    def get_target_storage(self):
        """
        Uses regex to get the target storage from reservoir files for the input period
        """
        year = self.year
        file = os.path.join(self.input_path, "reservoir_details.dat")
        with open(file, "r") as f:
            data = f.read()

        name_pattern = re.compile(r"\w+ Reservoir")
        target_pattern = re.compile(
            r"\d+\.?\d+ +\d\d +0\.\d +0\.\d +(\d+\.?\d+) +0\.\d"
        )
        name_matches = re.finditer(name_pattern, data)
        target_matches = re.finditer(target_pattern, data)
        if not os.path.isdir("./TargetStorage"):
            os.mkdir("./TargetStorage")
        with open(
            os.path.join("./TargetStorage", "target_storage_{}.csv".format(year)), "w+"
        ) as f:
            writer = csv.writer(f)
            writer.writerow(["Name", "Target"])
            for name, target in zip(name_matches, target_matches):
                res = name.group(0)
                targ = target.group(1)
                res = res.split()
                res = "_".join(res)
                writer.writerow([res, targ])

    def create_mass_balance_output(self):
        file = os.path.join(self.output_path, "mass_balance_vars.out")
        inflow_file = os.path.join(self.output_path, "res_inflow_breakdown.out")
        data = []
        inflow_data = []
        with open(file, "r") as f:
            for line in f:
                line = line.split()
                # when splitting on white space, the names get split
                # so joining the names with a underscore
                name = "_".join(line[:2])
                new_data = [name] + line[2:]
                data.append(new_data)
        df = pd.DataFrame.from_records(
            data,
            columns=[
                "Name",
                "CurFlow",
                "StPre",
                "Def",
                "Spill",
                "CurRel",
                "Evap",
                "StCur",
                "check",
                "st_flag",
                "lbound",
                "ubound",
            ],
            coerce_float=True,
        )
        with open(inflow_file, "r") as f:
            for line in f:
                line = line.split()
                # when splitting on white space, the names get split
                # so joining the names with a underscore
                name = "_".join(line[:2])
                new_data = [name] + line[2:]
                inflow_data.append(new_data)
        inflow_df = pd.DataFrame.from_records(
            inflow_data, columns=["Name", "UnCntrl", "Cntrl"], coerce_float=True
        )
        df = pd.concat([df, inflow_df.drop("Name", axis=1)], axis=1)
        type_change_columns = [
            "CurFlow",
            "StPre",
            "Def",
            "Spill",
            "CurRel",
            "Evap",
            "StCur",
            "check",
            "UnCntrl",
            "Cntrl",
        ]
        df[type_change_columns] = df[type_change_columns].astype(np.float)
        df["Run"] = df.index // 28
        df["VorD"] = df["Cntrl"].apply(lambda x: "V" if abs(x) < 0.001 else "D")
        df["DelSto"] = df["StPre"] - df["StCur"]
        df.to_csv(os.path.join(self.output_path, "mb_vars.csv"))

    def clear_objective_file(self):
        objective_output_file = f"./objective_output/{self.scen_name}.csv"
        with open(objective_output_file, "w") as f:
            pass
        
    def write_objective_value(self, index):
        obj = self.get_objective_value()
        objective_output_file = f"./objective_output/{self.scen_name}.csv"
        with open(objective_output_file, "a") as f:
            f.write(f"{index},{obj}\n")


def convert_seconds_to_minutes(seconds):
    return divmod(seconds, 60)


def print_scenario_start(model):
    print(
        "\n\t"
        + Fore.GREEN
        + Style.BRIGHT
        + "Solving scenario {}\n".format(model.scen_name)
        + Style.RESET_ALL
    )

def print_model_time_stats(time, iterations):
    avg_time = time / iterations
    minutes, seconds = convert_seconds_to_minutes(time)

    print("\n\tTime Statistics:")
    print(f"\t   Total time: {minutes:0.0f} minutes and {seconds:0.2f} seconds")
    print(f"\t   Number of Iterations: {int(iterations):12d}")
    print(f"\t   Average time per iteration: {avg_time:10.3f} seconds\n")


def run_single(args):
    method = args.get("method", "icorps")
    epsilon = float(args.get("epsilon", 0.005))

    m = COREGS(args)
    print_scenario_start(m)
    
    time1 = timer()
    if method in ["mhb", "mhp"]:
        m.run_FFSQP()
        iterations = 1
    elif method == "icorps":
        m.initialize()
        iterations = m.icorps(epsilon=epsilon)
    else:
        m.initialize()
        m.single_run()
        iterations = 1
    time2 = timer()
    
    print_model_time_stats(time2 - time1, iterations)

    return m


def run_rolling(args):
    method = args.get("method", "icorps")
    epsilon = float(args.get("epsilon", 0.005))
    one_run = args.get("one_run")

    if one_run:
        windows = 1 
    else:
        windows = 12

    for window in range(windows):
        if one_run:
            args["first"] = False
        else:
            args["first"] = window == 0

        if window > 0:
            args["start_month"] = "{:02d}".format(int(args["start_month"]) + 1)

        if args["start_month"] == "13":
            args["start_month"] = "01"
            args["start_year"] = str(int(args["start_year"]) + 1)
    
        m = run_single(args) 

        del m


if __name__ == "__main__":
    import warnings

    warnings.simplefilter("ignore")
    args = parse_args()
    rolling = args.get("rolling", None)
    
    if rolling:
        run_rolling(args)
    else:
        run_single(args)


    # if method in ["mhb", "mhp"]:
    #     if rolling:
    #         windows = 12
    #         init_month = int(args["start_month"])
    #         for window in range(windows):

    #             args["first"] = window == 0
    #             if window > 1:
    #                 args["start_month"] = "{:02d}".format(int(args["start_month"]) + 1)

    #             if args["start_month"] == "13":
    #                 args["start_month"] = "01"
    #                 args["start_year"] = str(int(args["start_year"]) + 1)

    #             m = COREGS(args)
    #             m.run_FFSQP()

    #             del m
    #     else:
    #         m = COREGS(args)
    #         m.run_FFSQP()

    # elif method  == "icorps":
    #     if rolling:
    #         windows = 12
    #         init_month = int(args["start_month"])
    #         for window in range(windows):

    #             args["first"] = window == 0
    #             if window > 0:
    #                 args["start_month"] = "{:02d}".format(int(args["start_month"]) + 1)
                
    #             if int(args["start_month"]) > 12:
    #                 args["start_month"] = str(int(args["start_month"]) - 12)
    #                 args["start_year"] = str(int(args["start_year"]) + 1)
    #             print(args["start_month"], args["start_year"])
    #             m = COREGS(args)
    #             m.initialize()

    #             print(
    #                 "\n\t"
    #                 + Fore.GREEN
    #                 + Style.BRIGHT
    #                 + "Solving scenario {}\n".format(m.scen_name)
    #                 + Style.RESET_ALL
    #             )

    #             start = timer()
    #             iterations = m.icorps(epsilon=epsilon)
    #             end = timer()
    #             time = end - start
    #             avg_time = time / iterations

    #             print("\n\tTime Statistics:")
    #             print("\t   Total time: {:26.3f} seconds".format(time))
    #             print("\t   Number of Iterations: {:12d}".format(int(iterations)))
    #             print("\t   Average time per iteration: {:10.3f} seconds\n".format(avg_time))

    #             terminal_histogram(m.violation_count)

    #             del m
    #     else:
    #         m = COREGS(args)
    #         m.initialize()

    #         print(
    #             "\n\t"
    #             + Fore.GREEN
    #             + Style.BRIGHT
    #             + "Solving scenario {}\n".format(m.scen_name)
    #             + Style.RESET_ALL
    #         )

    #         start = timer()
    #         iterations = m.icorps(epsilon=epsilon)
    #         end = timer()
    #         time = end - start
    #         avg_time = time / iterations
    #         total_change = (m.first_cost - m.last_cost) / m.first_cost * 100
    #         print("\n\tTime Statistics:")
    #         print("\t   Total time: {:26.3f} minutes".format(time / 60))
    #         print("\t   Number of Iterations: {:12d}".format(int(iterations)))
    #         print("\t   Average time per iteration: {:10.3f} seconds\n".format(avg_time))
    #         print(
    #             "\t"
    #             + Fore.GREEN
    #             + Style.BRIGHT
    #             + "Total Objective Improvement {:.1f}%\n".format(total_change)
    #             + Style.RESET_ALL
    #         )

    #         terminal_histogram(m.violation_count)


    # elif method == "single":
    #     m = COREGS(args)
    #     m.initialize()

    #     print(
    #         "\n\t"
    #         + Fore.GREEN
    #         + Style.BRIGHT
    #         + "Solving scenario {}\n".format(m.scen_name)
    #         + Style.RESET_ALL
    #     )

    #     start = timer()
    #     m.single_run()
    #     end = timer()
    #     time = end - start
    #     avg_time = time
    #     iterations = 1

    #     print("\n\tTime Statistics:")
    #     print("\t   Total time: {:26.3f} seconds".format(time))
    #     print("\t   Number of Iterations: {:12d}".format(iterations))
    #     print("\t   Average time per iteration: {:10.3f} seconds\n".format(avg_time))