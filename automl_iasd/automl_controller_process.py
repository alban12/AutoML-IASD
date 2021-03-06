import json
import subprocess
import click 
import time
import concurrent.futures
from queue import Queue
from pyspark.context import SparkContext
from pyspark import SQLContext, SparkConf
import os
import logging
import boto3

# Initialize a spark context to get the spark version to automatically download the right S3A package
sc_conf = SparkConf()
sc = SparkContext(conf=sc_conf)
spark_version = sc.version

AWS_ACCESS_KEY = os.getenv('AWS_ACCESS_KEY')
AWS_SECRET_KEY = os.getenv('AWS_SECRET_KEY')

logging.getLogger().setLevel(logging.INFO)

####################
# Define utilities #
####################

class ThreadPoolExecutorWithQueueSizeLimit(
    concurrent.futures.ThreadPoolExecutor):
	"""Thread pool that can handle size limit for task that are treated as being blocked by I/O."""
	def __init__(self, maxsize, *args, **kwargs):
		super(ThreadPoolExecutorWithQueueSizeLimit, self).__init__(*args, **kwargs)
		self._work_queue = Queue(maxsize=maxsize)

def getYARNApplicationID(app_name):
	"""Returns the YARN application ID."""
	state = 'RUNNING,ACCEPTED,FINISHED,KILLED,FAILED'
	out = subprocess.check_output(["yarn","application","-list", "-appStates",state], stderr=subprocess.DEVNULL, universal_newlines=True)
	lines = [x for x in out.split("\n")]
	application_id = ''
	for line in lines:
		if app_name in line:
			application_id = line.split('\t')[0]
			break
	return application_id

def getSparkJobFinalStatus(application_id):
	"""Return the state of the job."""
	out = subprocess.check_output(["yarn","application", "-status",application_id], stderr=subprocess.DEVNULL, universal_newlines=True)
	status_lines = out.split("\n")
	state = ''
	for line in status_lines:
		if len(line) > 15 and line[1:15] == "Final-State : ":
			state = line[15:]
			break
	return state

####################################
# Define parallelization functions #
####################################

def executeThread(app_name, spark_submit_cmd, error_log_dir,
        max_wait_time_job_start_s = 0):
	"""Launch the execution of a thread given a spark-submit command."""
	logging.info(f"Executing thread for {app_name}")
	cmd_output = subprocess.Popen(spark_submit_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
	for line in cmd_output.stdout:
		print(line)
	cmd_output.communicate()
	return True


def executeAllThreads(dict_spark_submit_cmds, error_log_dir, 
        dict_success_app=None):
	"""Create a pool of thread to execute all the given spark-submit commands."""
	if dict_success_app is None:
		dict_success_app = {app_name: False for app_name in dict_spark_submit_cmds.keys()}
		max_parallel=len(dict_spark_submit_cmds)
	with ThreadPoolExecutorWithQueueSizeLimit(maxsize=max_parallel, max_workers=max_parallel) as executor:
		future_to_app_name = {
			executor.submit(
				executeThread, app_name, spark_submit_cmd, error_log_dir
			): app_name for app_name, spark_submit_cmd in dict_spark_submit_cmds.items() if dict_success_app[app_name] == False
        }
		for future in concurrent.futures\
        		.as_completed(future_to_app_name):
			app_name = future_to_app_name[future]
			try:
				dict_success_app[app_name] = future.result()
			except Exception as exc:
				print('Subordinate task %s generated exception %s' % (app_name, exc))
				raise
	return dict_success_app

####################################
# 	     Controller Function       #
####################################

@click.command()
@click.option('--dataset', prompt="The name of the dataset to predict",\
				help='The path in s3 to the dataset (will be retrieve from s3)')
@click.option('--label_column_name', prompt="The name of the column to predict",\
				help='The name of the label column')
@click.option('--task', prompt="The task to perform.",\
				help='The task associated with the dataset, can either be - classification, multiclass_classification, regression')
@click.option('--metric', default="None",\
				help='The metric to use for the evaluation of the algorithms.')
@click.option('--budget', default=3,\
				help='The budget "n" allowed for the run. (decomposed as follow : n/3 to apply binaryOperions, n/3 for feature selection and n/3 for HPO')
@click.option('--training_only', default=False,\
				help='State if the provided dataset is only for training purpose.')
@click.option('--bucket_name',  prompt="The name of the bucket that holds the datasets.",\
				help='Name of the bucket that contains the dataset and that will hold the results.')
@click.option('--iam_role', default=None,\
				help='AWS Ressource name of the role created for the feature store.')
@click.option('--usable_memory', default="4g",\
				help='If run in local, the memory size that the program can use. A size too small might gives out of space error (default size is 4g)')
@click.option('--on_aws_cluster', default=False,\
				help='State if the script is launched on a AWS cluster')
def distribute_algorithms(dataset, label_column_name, task, metric, budget, training_only, bucket_name, iam_role, usable_memory, on_aws_cluster):
	"""Main function responsible for the creation of the commands and the calculation of the best scoring algorithm."""
	# Define the algorithms - in correspondance with the task  
	if task == "classification" or "multiclass_classification":
		algorithms = ["logistic_regression", 
			"random_forest", 
			"gradient_boosted_tree",  
			"perceptron_multilayer",
			"support_vector_machines",
			"factorization_machines"
		]
	elif task == "regression":
		algorithms = ["linear_regression",
			"generalized_linear_regression",
			"gradient_boosted_tree_regression",
			"factorization_machines_regression"
		]
	else:
		raise ValueError()

	# Create a unique YARN name 
	curr_timestamp = int(time.time()*1000)
	app_names = [dataset+label_column_name+algorithms[i]+str(curr_timestamp) for i in range(len(algorithms))]

	# Generate an instance name for the run
	process_instance_name = "automl_instance_{curr_timestamp}"

	# Affect an S3 path
	if training_only:
		dataset_path = f"s3://{bucket_name}/{dataset}/dataset/{dataset}.parquet/"
	else:
		dataset_path = f"s3://{bucket_name}/{dataset}/dataset/{dataset}_train.parquet/"
	automl_instance_model_path = f"{dataset}/models/{process_instance_name}"
 
	# Download s3a dependency and set the drviver memory if the program is not a run on an AWS cluster
	s3a_download = ""
	driver_memory = ""
	if not(on_aws_cluster):
		s3a_download = f"--packages org.apache.hadoop:hadoop-aws:{spark_version}"
		driver_memory = f"--driver-memory {usable_memory}"

	# Create the appropriate spark-submit command for each algorithm
	dict_spark_submit_cmds = dict()
	for i in range(len(algorithms)):
	    spark_submit_cmd = f"spark-submit {driver_memory} {s3a_download} --name {app_names[i]}" \
	    				f" --conf spark.yarn.dist.archives=../dist/automl-iasd-0.1.0.tar" \
	    				f"{task}_algorithms/{algorithms[i]}_process.py {dataset} {label_column_name}" \
	    				f"{task} {metric} {budget} {automl_instance_model_path} {training_only}" \
	    				f" {bucket_name} {iam_role} {AWS_ACCESS_KEY} {AWS_SECRET_KEY}"
	    dict_spark_submit_cmds[app_names[i]] = spark_submit_cmd

	# Launch the threadPool 
	logging.info("AutoML - Controller process : distributing subprocesses ... ")
	dict_success_app = executeAllThreads(dict_spark_submit_cmds, ".")
	logging.info("AutoML - All subordinates processes have finished")

	# Connect to the instance folder in the bucket to see which process has performed better
	s3 = boto3.client("s3")
	response = s3.list_objects_v2(Bucket=bucket_name , Prefix=automl_instance_model_path, MaxKeys=100 )

	# Retrieve all subfolders name of automl_instance_model_path in a list 
	scores = [(x["Key"].split(process_instance_name)[1].split("_")[0], \
				x["Key"].split(process_instance_name)[1].split("_")[1].split("/")[0]) for x in response["Contents"]]

	# Sort the algorithm by their scores
	scores.sort(key = lambda scores: scores[1])
	best_algorithm_with_metric = scores[-1]

	logging.info(f"The best algorithm is {best_algorithm_with_metric[0]} with the metric being : {best_algorithm_with_metric[1]}")

	return best_algorithm_with_metric

if __name__ == '__main__':
    distribute_algorithms()