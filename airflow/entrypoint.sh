#!/usr/bin/env bash
# Entry point for the Docker container

# Wait for PostgreSQL to be ready before running the init scripts
# echo "Waiting for PostgreSQL to be ready..."
# until psql -h timescaledb -U postgres -c '\l'; do
#   >&2 echo "PostgreSQL is unavailable - sleeping"
#   sleep 1
# done
# echo "PostgreSQL is ready."

# Add a delay to ensure all services are fully up before starting Airflow
echo "Sleeping for 30 seconds to ensure all services are up..."
sleep 30

# Check if init_dag_tables.py exists
if [ -f /opt/airflow/tools/init_dag_tables.py ]; then
    echo "init_dag_tables.py script found, running it..."
    # Run the custom initialization script
    python /opt/airflow/tools/init_dag_tables.py
    if [ $? -ne 0 ]; then
        echo "Failed to execute init_dag_tables.py script"
        exit 1
    fi
    echo "Finished running custom initialization script"
else
    echo "init_dag_tables.py script not found!"
fi

# Initialize the Airflow database
echo "Initializing the Airflow database..."
airflow db init
echo "Airflow database initialized."

# Create an admin user if it does not exist
echo "Creating Airflow admin user..."
airflow users create \
    --username admin \
    --password admin \
    --firstname Admin \
    --lastname User \
    --role Admin \
    --email admin@example.com
echo "Airflow admin user created."

# Start the scheduler and webserver in background
echo "Starting Airflow scheduler..."
airflow scheduler &

echo "Starting Airflow webserver..."
airflow webserver &

# Keep the container running
echo "Entering infinite loop to keep container running..."
tail -f /dev/null
