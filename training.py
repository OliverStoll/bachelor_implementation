from datetime import datetime
import tensorflow as tf
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import os
import sys
import keras_tuner as kt
from keras.callbacks import TensorBoard
from tensorboard.plugins.hparams import api as hp
from sklearn.preprocessing import MinMaxScaler, StandardScaler
from io import StringIO

from plotting import *  # evaluate_model_lstm, evaluate_model_fft, find_timestretches_from_indexes
from models import lstm_autoencoder_model, fft_autoencoder_model
from util.ml_calculations import *
from util.ml_callbacks import scheduler  # , tensor_callback
from util.config import c, client_config


# os.environ['CUDA_VISIBLE_DEVICES'] = '-1'  # disable GPU usage (IIoT)
anomalies = yaml.safe_load(open(f"configs/anomalies.yaml"))


class Training:
    def __init__(self, data_path, data_columns):
        # config values
        self.data_path = data_path
        self.dataset_name = data_path.split('/')[-2]
        self.experiment_name = data_path.split('/')[-1]
        self.sub_experiment_index = int(data_columns[0] / len(data_columns))
        self.data_columns = data_columns
        self.split_size = c.SPLIT
        self.batch_size = c.BATCH_SIZE
        self.val_split = c.VAL_SPLIT
        self.train_split = c.TRAIN_SPLIT
        self.threshold_deviations = c.THRESHOLD_DEVIATIONS
        self.thresholds = {'lstm': None, 'fft': None}
        self.verbose = 1
        self.is_until_failure = 'bearing' in data_path
        self.use_optimal_threshold = c.USE_OPTIMAL_THRESHOLD
        self.threshold_calc_period = c.THRESHOLD_CALCULATION_PERIOD
        # data
        self.labels = anomalies[self.dataset_name][self.experiment_name][
            f'bearing-{int(data_columns[0] / len(data_columns))}']
        self.data_3d, self.data_train_3d = self._load_and_normalize_data()
        self.data_2d = self.data_3d.reshape((-1, self.data_3d.shape[2]))
        self.fft_3d = fft_from_data(self.data_3d)
        self.fft_train_3d = fft_from_data(self.data_train_3d)
        self.fft_2d = self.fft_3d.reshape((-1, self.fft_3d.shape[2]))
        # models
        self.model_lstm = lstm_autoencoder_model()
        self.model_fft = fft_autoencoder_model()
        self.callbacks = [tf.keras.callbacks.LearningRateScheduler(scheduler)]  # initialize the learning rate scheduler
        # results
        self.history_lstm = {'loss': [], 'val_loss': []}
        self.history_fft = {'loss': [], 'val_loss': []}
        self.mses = {'lstm': [], 'fft': []}
        self.f1s = {'lstm': None, 'fft': None}

    def _load_and_normalize_data(self):
        """
        Prepare the data for training and evaluation. All features need to be extracted and named.

        The data is normalized and possibly split into training and testing data 80/20.

        :return: the full data, the training data and the 3d array of the training data
        """

        # read the data from the csv file
        path = f"{self.data_path}_{self.split_size}.csv"
        if self.split_size == 20480 or self.split_size == 800:
            path = f"{self.data_path}_full.csv"  # for the full dataset

        df = pd.read_csv(path, usecols=self.data_columns)

        # drop the last rows that are not a full split anymore
        if len(df) % self.split_size != 0:
            df = df.iloc[:-(len(df) % self.split_size)]

        # split percentage of data as training data, if specified as < 1
        split_len = int(len(df) * self.train_split) \
                    + (self.split_size - int(len(df) * self.train_split) % self.split_size)

        # split the data frame into multiple lists
        data = df.iloc[:, :].values
        train_data = df[:split_len].iloc[:, :].values

        # normalize the data
        scaler = StandardScaler()  # MinMaxScaler()
        data = scaler.fit_transform(data)
        train_data = scaler.transform(train_data)

        # reshape data to 3d arrays with the second value equal to the number of timesteps (SPLIT)
        data_3d = data.reshape((-1, self.split_size, data.shape[1]))
        train_data_3d = train_data.reshape((-1, self.split_size, train_data.shape[1]))

        return data_3d, train_data_3d

    def load_models(self, dir_path):
        """
        Load the models from the specified directory.

        :param dir_path: the directory path to the models
        :return: the model and the fft model
        """

        # load the models
        self.model_lstm = tf.keras.models.load_model(f"{dir_path}/lstm.h5")
        self.model_fft = tf.keras.models.load_model(f"{dir_path}/fft.h5")

    def save_models(self, dir_path):
        """
        Save the models to the specified directory.

        :param dir_path: the directory path to the models
        """

        # save the models
        self.model_lstm.save(f"{dir_path}/lstm.h5")
        self.model_fft.save(f"{dir_path}/fft.h5")

    def tune_models(self, replace_models=False):
        print("TUNING MODELS - REDIRECTING STDOUT")

        # save stdout to a file
        old_stdout = sys.stdout
        # delete the old log file and create folder if not existing
        log_path = f"hyper_tuning/tuning_log.txt"
        os.makedirs("hyper_tuning", exist_ok=True)
        if os.path.exists(log_path):
            os.remove(log_path)
        sys.stdout = open(f"hyper_tuning/tuning_log.txt", "w")

        tb_lstm = TensorBoard(log_dir=f"hyper_tuning/logs/lstm")
        tb_fft = TensorBoard(log_dir=f"hyper_tuning/logs/fft")
        tuner_lstm = kt.RandomSearch(lstm_autoencoder_model, objective='val_loss',
                                     project_name="hyper_tuning/lstm", max_trials=1000000)
        tuner_fft = kt.RandomSearch(fft_autoencoder_model, objective='val_loss',
                                    project_name="hyper_tuning/fft", max_trials=1000000)
        tuner_lstm.search(self.data_train_3d,
                          self.data_train_3d,
                          epochs=50,
                          batch_size=self.batch_size,
                          validation_split=self.val_split,
                          callbacks=[tb_lstm],
                          verbose=2)
        tuner_fft.search(self.fft_train_3d,
                         self.fft_train_3d,
                         epochs=50,
                         batch_size=self.batch_size,
                         validation_split=self.val_split,
                         callbacks=[tb_fft])
        tuner_lstm.results_summary(num_trials=1)
        print()
        tuner_fft.results_summary(num_trials=1)

        # restore stdout
        sys.stdout = old_stdout

        if replace_models:
            self.model_lstm = tuner_lstm.get_best_models(num_models=1)[0]
            self.model_fft = tuner_fft.get_best_models(num_models=1)[0]

    def train_models(self, epochs=1):
        """
        Train the model for a given number of epochs. The training data needs to be formatted as a 3D array.

        :param epochs: the number of epochs to train for
        :return: the trained model
        """

        # train the models
        _history_lstm = self.model_lstm.fit(self.data_train_3d,
                                            self.data_train_3d,
                                            epochs=epochs,
                                            batch_size=self.batch_size,
                                            callbacks=self.callbacks,
                                            validation_split=self.val_split,
                                            verbose=self.verbose).history
        _history_fft = self.model_fft.fit(self.fft_train_3d,
                                          self.fft_train_3d,
                                          epochs=epochs,
                                          batch_size=self.batch_size,
                                          callbacks=self.callbacks,
                                          validation_split=self.val_split,
                                          verbose=self.verbose).history

        self.history_lstm['loss'] += _history_lstm['loss']
        self.history_lstm['val_loss'] += _history_lstm['val_loss']
        self.history_fft['loss'] += _history_fft['loss']
        self.history_fft['val_loss'] += _history_fft['val_loss']

    def calculate_anom_score(self, mean_over_period=True):

        # calculate the anomaly scores
        data_pred_2d = self.model_lstm.predict(self.data_3d, verbose=0).reshape((-1, self.data_3d.shape[2]))
        fft_pred_2d = self.model_fft.predict(self.fft_3d, verbose=0).reshape((-1, self.fft_3d.shape[2]))
        self.mses = {'lstm': ((self.data_2d - data_pred_2d) ** 2).mean(axis=1),
                     'fft': ((self.fft_2d - fft_pred_2d) ** 2).mean(axis=1)}

        # calculate the mean of each group
        if mean_over_period:
            # group the mse scores in groups of split
            mse_lstm_grouped = np.array([self.mses['lstm'][i:i + self.split_size] for i in
                                         range(0, len(self.mses['lstm']), self.split_size)])
            mse_fft_grouped = np.array([self.mses['fft'][i:i + self.split_size] for i in
                                        range(0, len(self.mses['fft']), self.split_size)])

            self.mses = {'lstm': np.mean(mse_lstm_grouped, axis=1),
                         'fft': np.mean(mse_fft_grouped, axis=1)}

    def calculate_threshold_from_standarddeviation(self):
        for model_type in ['lstm', 'fft']:
            start_index = int(self.threshold_calc_period[0] * self.split_size)
            end_index = int(self.threshold_calc_period[1] * self.split_size)
            mse_period = self.mses[model_type][start_index:end_index]
            # get the mean and standard deviation of the mse scores
            mean = np.array(mse_period).mean()
            std = np.array(mse_period).std()
            self.thresholds[model_type] = mean + std * self.threshold_deviations

    def evaluate(self, show_all=False, show_infos=False, show_as=False, show_preds=False, show_roc=False,
                 show_losses=False):
        """
        Evaluate the models seperately.

        This is done by functionality in evaluation.py
        """

        self.calculate_anom_score()
        self.calculate_threshold_from_standarddeviation()

        # plot general information
        general_plotter = MiscPlotter(trainer=self)
        if show_losses or show_all:
            general_plotter.plot_losses()
        if show_infos or show_all:
            general_plotter.plot_infotable()

        # calculate the AUC for both models, and find the theoretically optimal threshold
        roc_plotter = RocPlotter()
        for type in ['lstm', 'fft']:
            fps, tps, auc, f1_max = calculate_auc(trainer=self, mse=self.mses[type],
                                                  is_until_failure=self.is_until_failure)
            if self.use_optimal_threshold:
                self.thresholds[type] = f1_max[2]  # use the optimal threshold as the threshold
            roc_plotter.plot_single_roc(fps=fps, tps=tps, auc=auc, f1_max=f1_max)

        # calculate the F1 score for both models
        for type in ['lstm', 'fft']:
            self.f1s[type] = calculate_f1(threshold=self.thresholds[type], mse=self.mses[type], labels=self.labels,
                                         is_until_failure=self.is_until_failure)
            self.f1s[type] = round(self.f1s[type], 4)
            print(f"{type: <4} F1: {self.f1s[type]:.3f}")

        # plot the ROC curves
        if show_roc or show_all:
            roc_plotter.show()

        # plot the anomaly scores
        if show_as or show_all:
            general_plotter.plot_anomaly_scores(thresholds=self.thresholds)

        # get all time-periods where the anomaly score is above the threshold
        anomaly_times = {}
        for type in ['lstm', 'fft']:
            anomaly_sample_indexes = get_anomaly_indexes(threshold=self.thresholds[type], mse=self.mses[type],
                                                         is_until_failure=self.is_until_failure)
            anomaly_times[type] = get_timetuples_from_indexes(indexes=anomaly_sample_indexes,
                                                              max_index=self.data_3d.shape[0])

        # plot the anomaly times
        if show_preds or show_all:
            PredictionsPlotter(file_path=f"{self.data_path}_{self.split_size}.csv",
                               sub_experiment_index=self.sub_experiment_index,
                               ylim=c.PLOT_YLIM_VIBRATION,
                               features=len(self.data_columns),
                               anomalies_real=anomalies[self.dataset_name][self.experiment_name],
                               anomalies_pred_lstm=anomaly_times['lstm'],
                               anomalies_pred_fft=anomaly_times['fft'],
                               ).plot_experiment()


if __name__ == '__main__':
    trainer = Training(data_path=c.CLIENT_1['DATASET_PATH'], data_columns=c.CLIENT_1['DATASET_COLUMNS'])
    # trainer.tune_models()
    trainer.train_models(epochs=10)
    # trainer.save_models(dir_path=c.CLIENT_1['MODEL_PATH'])
    # trainer.load_models(dir_path="model/bearing/sabtain_2")
    trainer.evaluate(show_preds=True, show_roc=True, show_infos=True, show_as=True)
