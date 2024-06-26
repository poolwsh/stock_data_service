from __future__ import annotations

import os
import sys
import socket
import yfinance as yf
import pandas as pd
from typing import List
from pathlib import Path
from datetime import timedelta, datetime
from airflow.models.dag import DAG
from airflow.exceptions import AirflowException
from airflow.operators.python import PythonOperator
from airflow.utils.dates import days_ago
import psycopg2.extras
import finnhub

from dags.utils.db import PGEngine, task_cache_conn
from dags.utils.logger import logger
from dags.dg_ak.utils.dg_ak_util_funcs import DgAkUtilFuncs as dguf
from dags.dg_fy.utils.dg_fy_config import dgfy_config as con


finnhub_client = finnhub.Client(api_key='cp71fvpr01qpb9rafj6gcp71fvpr01qpb9rafj70')

current_path = Path(__file__).resolve().parent 
config_path = current_path / 'dg_fy_s-us_config.py'
sys.path.append(config_path.parent.as_posix())
ak_cols_config_dict = dguf.load_ak_cols_config(config_path.as_posix())

ARG_LIST_CACHE_PREFIX = "dg_fy_s-us_arg_list"
FAILED_STOCKS_CACHE_PREFIX = "dg_fy_s-us_failed_stocks"

TRACING_TABLE_NAME = 'dg_fy_tracing_s_us'
TRADE_DATE_TABLE_NAME = 'dg_fy_stock_us_trade_date'
STOCK_SYMBOL_TABLE = 'dg_fh_s_us_symbol'
STOCK_DATA_KEY = 'stock_us_hist_daily_bfq'

DEBUG_MODE = con.DEBUG_MODE
DEFAULT_END_DATE = dguf.format_td10(datetime.now())
DEFAULT_START_DATE = con.US_DEFAULT_START_DATE
BATCH_SIZE = 50000  
ROLLBACK_DAYS = 15



def fetch_us_symbols():
    us_symbols = finnhub_client.stock_symbols('US')
    symbol_list = [(symbol['symbol'], symbol['currency'], symbol['description'], symbol['displaySymbol'],
                    symbol['figi'], symbol['isin'], symbol['mic'], symbol['shareClassFIGI'],
                    symbol['symbol2'], symbol['type']) for symbol in us_symbols]
    return symbol_list

def fetch_yfinance_data(ticker: str, start_date: str, end_date: str) -> pd.DataFrame:
    stock = yf.Ticker(ticker)
    df = stock.history(start=start_date, end=end_date, auto_adjust=False)
    if df.empty:
        raise AirflowException(f"No data found for ticker {ticker} between {start_date} and {end_date}")
    return df

def insert_symbol_data_to_db(symbol_data_list: list[tuple[str, str, str, str, str, str, str, str, str, str]]):
    try:
        with PGEngine.managed_conn() as conn:
            with conn.cursor() as cursor:
                sql = f"""
                    INSERT INTO {STOCK_SYMBOL_TABLE} (symbol, currency, description, displaySymbol, figi, isin, mic, shareClassFIGI, symbol2, type, create_time, update_time)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                    ON CONFLICT (symbol) DO UPDATE 
                    SET currency = EXCLUDED.currency,
                        description = EXCLUDED.description,
                        displaySymbol = EXCLUDED.displaySymbol,
                        figi = EXCLUDED.figi,
                        isin = EXCLUDED.isin,
                        mic = EXCLUDED.mic,
                        shareClassFIGI = EXCLUDED.shareClassFIGI,
                        symbol2 = EXCLUDED.symbol2,
                        type = EXCLUDED.type,
                        update_time = EXCLUDED.update_time;
                """
                cursor.executemany(sql, symbol_data_list)
                conn.commit()
                logger.info(f"Symbol data inserted into {STOCK_SYMBOL_TABLE} successfully.")
    except Exception as e:
        logger.error(f"Failed to insert symbol data into {STOCK_SYMBOL_TABLE}: {e}")
        raise AirflowException(e)

def update_tracing_table_bulk(updates: List[tuple]):
    conn = None
    try:
        sql = f"""
                INSERT INTO {TRACING_TABLE_NAME} (symbol, last_td, create_time, update_time, host_name)
                VALUES %s
                ON CONFLICT (symbol) DO UPDATE 
                SET last_td = EXCLUDED.last_td, update_time = EXCLUDED.update_time, host_name = EXCLUDED.host_name;
            """
        hostname = os.getenv('HOSTNAME', socket.gethostname())
        values = [(symbol, last_td, datetime.now(), datetime.now(), hostname) for symbol, last_td in updates]

        with PGEngine.managed_conn() as conn:
            with conn.cursor() as cursor:
                psycopg2.extras.execute_values(cursor, sql, values)
            conn.commit()
            logger.info(f"Tracing data updated for {len(updates)} records in {TRACING_TABLE_NAME}.")
    except Exception as e:
        logger.error(f"Failed to update tracing table in bulk: {e}")
        raise AirflowException(e)

def update_trade_dates(conn, trade_dates):
    insert_date_sql = f"""
        INSERT INTO {TRADE_DATE_TABLE_NAME} (trade_date, create_time, update_time)
        VALUES (%s, NOW(), NOW())
        ON CONFLICT (trade_date) DO NOTHING;
    """
    with conn.cursor() as cursor:
        cursor.executemany(insert_date_sql, [(date,) for date in trade_dates])
    conn.commit()

def prepare_arg_list():
    with PGEngine.managed_conn() as conn:
        tracing_df = dguf.get_tracing_data_df(conn, TRACING_TABLE_NAME)
        tracing_df['last_td'] = tracing_df['last_td'].apply(dguf.format_td10)
        tracing_dict = dict(zip(tracing_df['symbol'].values, tracing_df['last_td'].values))
        if DEBUG_MODE:
            logger.debug(f'tracing_dict:\n{list(tracing_dict.items())[:3]}')

        symbol_data_list = fetch_us_symbols()
        insert_symbol_data_to_db(symbol_data_list)
        s_code_list = [item[0] for item in symbol_data_list]
        
        arg_list = []
        for s_code in s_code_list:
            start_date = tracing_dict.get(s_code, DEFAULT_START_DATE)

            if start_date != DEFAULT_START_DATE:
                start_date = (datetime.strptime(str(start_date), '%Y-%m-%d') - timedelta(days=ROLLBACK_DAYS)).strftime('%Y-%m-%d')
            arg_list.append((s_code, dguf.format_td10(start_date), DEFAULT_END_DATE))

        redis_key = f"{ARG_LIST_CACHE_PREFIX}@{STOCK_DATA_KEY}"
        dguf.write_list_to_redis(redis_key, arg_list, task_cache_conn)
        logger.info(f"Argument list for {STOCK_DATA_KEY} has been prepared and cached.")

def process_stock_data():
    try:
        with PGEngine.managed_conn() as conn:
            logger.info(f"Starting to save data for {STOCK_DATA_KEY}")
            redis_key = f"{ARG_LIST_CACHE_PREFIX}@{STOCK_DATA_KEY}"
            arg_list = dguf.read_list_from_redis(redis_key, task_cache_conn)

            if not arg_list:
                raise AirflowException(f"No arguments available for {STOCK_DATA_KEY}, skipping data fetch.")

            if DEBUG_MODE:
                logger.debug(f"Config dictionary for {STOCK_DATA_KEY}: {ak_cols_config_dict}")

            total_codes = len(arg_list)
            all_data = []
            total_rows = 0
            all_trade_dates = set()
            failed_stocks = []

            for index, (s_code, start_date, end_date) in enumerate(arg_list):
                try:
                    logger.info(f'({index + 1}/{total_codes}) Fetching data for s_code={s_code} from {start_date} to {end_date}')
                    stock_data_df = fetch_yfinance_data(s_code, start_date, end_date)

                    if not stock_data_df.empty:
                        stock_data_df.rename(columns=dguf.get_col_dict(ak_cols_config_dict[STOCK_DATA_KEY]), inplace=True)
                        stock_data_df['symbol'] = s_code
                        stock_data_df['td'] = stock_data_df.index
                        stock_data_df.reset_index(drop=True, inplace=True)
                        if DEBUG_MODE:
                            logger.debug(stock_data_df.head(3))
                        all_data.append(stock_data_df)
                        total_rows += len(stock_data_df)
                        all_trade_dates.update(stock_data_df['td'].dt.strftime('%Y-%m-%d').unique())
                        if DEBUG_MODE:
                            logger.debug(f's_code={s_code}, len(stock_data_df)={len(stock_data_df)}, len(all_data)={len(all_data)}, total_rows={total_rows}')
                    else:
                        failed_stocks.append(arg_list[index])

                    if total_rows >= BATCH_SIZE or (index + 1) == total_codes:
                        _combined_df = pd.concat(all_data, ignore_index=True)
                        process_batch_data(_combined_df, all_trade_dates, conn)
                        all_data = []
                        total_rows = 0
                        all_trade_dates.clear()

                except Exception as e:
                    logger.error(f"Failed to process data for s_code={s_code}: {e}")
                    failed_stocks.append(arg_list[index])

            if failed_stocks:
                dguf.write_list_to_redis(FAILED_STOCKS_CACHE_PREFIX, failed_stocks, task_cache_conn)
                logger.info(f"Failed stocks: {failed_stocks}")

    except Exception as e:
        logger.error(f"Failed to process data for {STOCK_DATA_KEY}: {e}")
        raise AirflowException(e)

def process_batch_data(combined_df, all_trade_dates, conn):
    if DEBUG_MODE:
        logger.debug(f"Combined DataFrame columns for {STOCK_DATA_KEY}: {combined_df.columns}")

    combined_df['symbol'] = combined_df['symbol'].astype(str)
    combined_df = dguf.convert_columns(combined_df, f'dg_fy_{STOCK_DATA_KEY}', conn, task_cache_conn)
    if 'td' in combined_df.columns:
        combined_df['td'] = pd.to_datetime(combined_df['td'], errors='coerce').dt.strftime('%Y-%m-%d')
    if DEBUG_MODE:
        logger.debug(f'starting write {len(combined_df)} data to csv.')
    temp_csv_path = dguf.save_data_to_csv(combined_df, f'{STOCK_DATA_KEY}')
    if temp_csv_path is None:
        raise AirflowException(f"No CSV file created for {STOCK_DATA_KEY}, skipping database insertion.")
    dguf.insert_data_from_csv(conn, temp_csv_path, f'dg_fy_{STOCK_DATA_KEY}', task_cache_conn)
    update_trade_dates(conn, all_trade_dates)
    updates = combined_df.groupby('symbol')['td'].max().reset_index().values.tolist()
    update_tracing_table_bulk(updates)

def retry_failed_stocks():
    try:
        logger.info(f"Retrying failed stocks for {STOCK_DATA_KEY}")
        failed_stocks = dguf.read_list_from_redis(FAILED_STOCKS_CACHE_PREFIX, task_cache_conn)
        if not failed_stocks:
            logger.info("No failed stocks to retry.")
            return

        logger.info(f"Failed stocks detected: {failed_stocks}")

        if failed_stocks:
            formatted_failed_stocks = "\n".join([str(index) for index in failed_stocks])
            logger.warning(f"Warning: There are failed indexes that need to be retried:\n{formatted_failed_stocks}")

    except Exception as e:
        logger.error(f"Failed to retry stocks for {STOCK_DATA_KEY}: {e}")
        raise AirflowException(e)

def generate_dag_name(stock_func) -> str:
    return f"美股行情-{stock_func}"

def generate_dag(stock_func):
    logger.info(f"Generating DAG for {stock_func}")
    default_args = {
        'owner': con.DEFAULT_OWNER,
        'depends_on_past': False,
        'email': [con.DEFAULT_EMAIL],
        'email_on_failure': False,
        'email_on_retry': False,
        'retries': con.DEFAULT_RETRIES,
        'retry_delay': timedelta(minutes=con.DEFAULT_RETRY_DELAY)
    }

    dag_name = generate_dag_name(stock_func)

    dag = DAG(
        dag_name,
        default_args=default_args,
        description=f'利用yfinance的函数{stock_func}下载美股行情相关数据',
        start_date=days_ago(1),
        schedule=dguf.generate_random_minute_schedule(hour=12), 
        catchup=False,
        tags=['yfinance', 'store_daily', '美股行情'],
        max_active_runs=1,
        params={},
    )

    tasks = {
        'prepare_arg_list': PythonOperator(
            task_id=f'prepare_arg_list_{stock_func}',
            python_callable=prepare_arg_list,
            op_kwargs={},
            dag=dag,
        ),
        'process_stock_data': PythonOperator(
            task_id=f'process_stock_data_{stock_func}',
            python_callable=process_stock_data,
            op_kwargs={},
            dag=dag,
        ),
        'retry_failed_stocks': PythonOperator(
            task_id=f'retry_failed_stocks_{stock_func}',
            python_callable=retry_failed_stocks,
            op_kwargs={},
            dag=dag,
        ),
    }
    tasks['prepare_arg_list'] >> tasks['process_stock_data'] >> tasks['retry_failed_stocks']
    return dag

def create_dags(STOCK_DATA_KEY):
    globals()[f'dg_{STOCK_DATA_KEY}'] = generate_dag(STOCK_DATA_KEY)
    logger.info(f"DAG for {STOCK_DATA_KEY} successfully created and registered.")

create_dags('us_stock_data')
