import numpy as np
import pandas as pd
import copy
pd.options.mode.chained_assignment = None
import matplotlib.pyplot as plt
import seaborn as sns
from category_encoders import CatBoostEncoder
from scipy.spatial.distance import jensenshannon
from scipy.stats import gaussian_kde
from sklearn.model_selection import RandomizedSearchCV, train_test_split
from sklearn.ensemble import RandomForestRegressor, RandomForestClassifier
from sklearn.metrics import (r2_score, mean_absolute_error, precision_score,
                             recall_score, accuracy_score, f1_score,
                             roc_auc_score)
from sklearn.utils import shuffle

import logging
logger = logging.getLogger(__name__)


class DataDriftDetector:
    """Compare differences between 2 datasets

    DataDriftDetector creates useful methods to compare 2 datasets,
    created to allow ease of measuring the fidelity between 2 datasets.

    Methods
    ----
    calculate_drift:
        Calculates the distribution distance for each column between the
        datasets with the jensen shannon metric

    plot_numeric_to_numeric:
        Creates a pairplot between the 2 datasets

    plot_categorical_to_numeric:
        Creates a pairgrid violin plot between the 2 datasets

    plot_categorical:
        Creates a proportion histogram between the 2 datasets for categorical
        columns

    compare_ml_efficacy:
        Compares the ML efficacy of a model built between the 2 datasets

    Args
    ----
    df_prior: <pandas.DataFrame>
        Pandas dataframe of the prior dataset. In practice, this would be the
        dataset used to train a live model

    df_post: <pandas.DataFrame>
        Pandas dataframe of the post dataset. In practice, this would be the
        current dataset that's flowing into a live model

    categorical_columns: <list of str>
        A list of categorical columns in the dataset, will be determined by
        column types if not provided

    numeric_columns: <list of str>
        A list of numeric columns in the dataset, will be determined by
        column types if not provided

    """
    def __init__(self,
                 df_prior,
                 df_post,
                 categorical_columns=None,
                 numeric_columns=None):
        assert isinstance(df_prior, pd.DataFrame),\
            "df_prior should be a pandas dataframe"
        assert isinstance(df_post, pd.DataFrame),\
            "df_prior should be a pandas dataframe"
        assert all(df_prior.columns == df_post.columns),\
            "df_prior and df_post should have the same column names"
        assert all(df_prior.dtypes == df_post.dtypes),\
            "df_prior and df_post should have the same column types"
        assert isinstance(categorical_columns, (list, type(None))),\
            "categorical_columns should be of type list"
        assert isinstance(numeric_columns, (list, type(None))),\
            "numeric_columns should be of type list"

        df_prior_ = copy.deepcopy(df_prior)
        df_post_ = copy.deepcopy(df_post)

        if categorical_columns is None:
            categorical_columns = (
                [c for c in df_prior_.columns if
                df_prior_.dtypes[c] == 'object']
            )
            logger.info(
                "Identified categorical column(s): ",
                categorical_columns
            )

        df_prior_[categorical_columns] = (
            df_prior_[categorical_columns].astype(str)
        )
        df_post_[categorical_columns] = (
            df_post_[categorical_columns].astype(str)
        )

        if numeric_columns is None:
            num_types = ['float64','float32','int32','int64','uint8']
            numeric_columns = (
                [c for c in df_prior_.columns if
                 df_prior_.dtypes[c] in num_types]
            )
            logger.info("Identified numeric column(s): ", numeric_columns)

        df_prior_[numeric_columns] = df_prior_[numeric_columns].astype(float)
        df_post_[numeric_columns] = df_post_[numeric_columns].astype(float)

        self.categorical_columns = categorical_columns
        self.numeric_columns = numeric_columns

        self.df_prior = df_prior_
        self.df_post = df_post_


    def calculate_drift(self):
        """Calculates the jensen shannon distance between the 2 datasets

        For categorical columns, the probability of each category will be
        computed separately for `df_prior` and `df_post`, and the jensen
        shannon distance between the 2 probability arrays will be computed. For
        numeric columns, the values will first be fitted into a gaussian KDE
        separately for `df_prior` and `df_post`, and a probability array
        will be sampled from them and compared with the jensen shannon distance

        Returns
        ----
        Sorted list of tuples containing the column name followed by the
        computed jensen shannon distance
        """
        cat_res = {}
        num_res = {}
        STEPS = 100

        for col in self.categorical_columns:
            # to ensure similar order, concat before computing probability
            col_prior = self.df_prior[col].to_frame()
            col_post = self.df_post[col].to_frame()
            col_prior['_source'] = 'prior'
            col_post['_source'] = 'post'

            col_ = pd.concat([col_prior, col_post], ignore_index=True)

            # aggregate and convert to probability array
            arr = (col_.groupby([col, '_source'])
                       .size()
                       .to_frame()
                       .reset_index()
                       .pivot(index=col, columns='_source')
                       .droplevel(0, axis=1)
                  )
            arr_ = arr.div(arr.sum(axis=0),axis=1)
            arr_.fillna(0, inplace=True)

            # calculate distance
            d = jensenshannon(arr_['prior'].to_numpy(),
                              arr_['post'].to_numpy())

            cat_res.update({col: d})

        for col in self.numeric_columns:
            # fit gaussian_kde
            col_prior = self.df_prior[col]
            col_post = self.df_post[col]
            kde_prior = gaussian_kde(col_prior)
            kde_post = gaussian_kde(col_post)

            # get range of values
            min_ = min(col_prior.min(), col_post.min())
            max_ = max(col_prior.max(), col_post.max())
            range_ = np.linspace(start=min_, stop=max_, num=STEPS)

            # sample range from KDE
            arr_prior_ = kde_prior(range_)
            arr_post_ = kde_post(range_)

            arr_prior = arr_prior_ / np.sum(arr_prior_)
            arr_post = arr_post_ / np.sum(arr_post_)

            # calculate js d
            d = jensenshannon(arr_prior, arr_post)

            num_res.update({col: d})

        if len(num_res) > 0:
            num_res = sorted(num_res.items(), key=lambda x:x[1], reverse=True)
        if len(cat_res) > 0:
            cat_res = sorted(cat_res.items(), key=lambda x:x[1], reverse=True)

        return {'categorical': cat_res, 'numerical': num_res}


    def plot_categorical_to_numeric(self,
                                    plot_categorical_columns=None,
                                    plot_numeric_columns=None,
                                    categorical_on_y_axis=True,
                                    height=4,
                                    aspect=1.0):
        """Plots charts to compare categorical to numeric columns pairwise.

        Plots a pairgrid violin plot of categorical columns to numeric
        columns, split and colored by the source of datasets

        Args
        ----
        plot_categorical_columns: <list of str>
            List of categorical columns to plot, uses all if no specified

        plot_numeric_columns: <list of str>
            List of numeric columns to plot, uses all if not specified

        categorical_on_y_axis: <boolean>
            Determines layout of resulting image - if True, categorical
            columns will be on the y axis

        height: <int>
            Height (in inches) of each facet

        aspect: <float>
            Aspect * height gives the width (in inches) of each facet.
        Returns
        ----
        Resulting plot
        """
        assert isinstance(plot_categorical_columns, (list, type(None))),\
            "plot_categorical_columns should be of type list"
        assert isinstance(plot_numeric_columns, (list, type(None))),\
            "plot_numeric_columns should be of type list"
        assert isinstance(categorical_on_y_axis, bool),\
            "categorical_on_y_axis should be a boolean value"

        df_prior = self.df_prior.copy()
        df_post = self.df_post.copy()

        col_nunique = df_prior.nunique()

        if plot_categorical_columns is None:
            plot_categorical_columns = (
                [col for col in col_nunique.index if
                 (col_nunique[col] <= 20) & (col in self.categorical_columns)]
            )

        if plot_numeric_columns is None:
            plot_numeric_columns = self.numeric_columns

        df_prior["_source"] = "Prior"
        df_post["_source"] = "Post"

        plot_df = pd.concat([df_prior, df_post])

        logger.info(
            "Plotting the following categorical column(s):",
            plot_categorical_columns,
            "Against the following numeric column(s):",
             plot_numeric_columns,
             "Categorical columns with high cardinality (>20 unique values)",
             "are not plotted."
        )

        # violinplot does not treat numeric string cols as string - error
        # sln: added a `_` to ensure it is read as a string
        plot_df[plot_categorical_columns] = (
            plot_df[plot_categorical_columns].astype(str) + "_"
        )

        if categorical_on_y_axis:
            y_cols = plot_categorical_columns
            x_cols = plot_numeric_columns
        else:
            y_cols = plot_numeric_columns
            x_cols = plot_categorical_columns

        g = sns.PairGrid(data=plot_df,
                         x_vars=x_cols,
                         y_vars=y_cols,
                         hue='_source',
                         height=height,
                         aspect=aspect)

        g.map(sns.violinplot,
              split=True,
              palette="muted",
              bw=0.1)

        plt.legend()

        return g


    def plot_numeric_to_numeric(self,
                                plot_numeric_columns=None,
                                alpha=1.0):
        """Plots charts to compare numeric columns pairwise.

        Plots a pairplot (from seaborn) of numeric columns, with a distribution
        plot on the diagonal and a scatter plot for all other charts

        Args
        ----
        plot_numeric_columns: <list of str>
            List of numeric columns to plot, uses all if not specified

        alpha: <float>
            Transparency of the scatter plot

        Returns
        ----
        Resulting plot
        """
        assert isinstance(plot_numeric_columns, (list, type(None))),\
            "plot_numeric_columns should be of type list"

        if plot_numeric_columns is None:
            plot_numeric_columns = self.numeric_columns

        df_prior = self.df_prior[plot_numeric_columns].copy()
        df_post = self.df_post[plot_numeric_columns].copy()

        df_prior['_source'] = "Prior"
        df_post['_source'] = "Post"

        plot_df = pd.concat([df_prior, df_post])

        logger.info(
            "Plotting the following numeric column(s):", plot_numeric_columns
        )

        g = sns.pairplot(data=plot_df,
                         hue='_source',
                         plot_kws={'alpha': alpha})

        return g


    def plot_categorical(self, plot_categorical_columns=None):
        """Plot histograms to compare categorical columns

        Args
        ----
        plot_categorical_columns: <list of str>
            List of categorical columns to plot, uses all if no specified

        Returns
        ----
        Resulting plot

        """
        assert isinstance(plot_categorical_columns, (list, type(None))),\
            "plot_categorical_columns should be of type list"

        col_nunique = self.df_prior.nunique()
        if plot_categorical_columns is None:
            plot_categorical_columns = (
                [col for col in col_nunique.index if
                 (col_nunique[col] <= 20) & (col in self.categorical_columns)]
            )

        logger.info(
            "Plotting the following categorical column(s):",
            plot_categorical_columns
        )

        fig, ax = plt.subplots(len(plot_categorical_columns), 2,
                               figsize=(10, 5*len(plot_categorical_columns)))

        for i, col in enumerate(plot_categorical_columns):

            if len(plot_categorical_columns) == 1:
                _ax0 = ax[0]
                _ax1 = ax[1]
            elif len(plot_categorical_columns) > 1:
                _ax0 = ax[i, 0]
                _ax1 = ax[i, 1]

            (self.df_prior[col].value_counts(normalize=True)
                               .rename("Proportion")
                               .sort_index()
                               .reset_index()
                               .pipe((sns.barplot, "data"),
                                     x="index", y="Proportion", ax=_ax0))
            _ax0.set_title(col + ", prior")
            _ax0.set(xlabel=col)
            (self.df_post[col].value_counts(normalize=True)
                              .rename("Proportion")
                              .sort_index()
                              .reset_index()
                              .pipe((sns.barplot, "data"),
                                    x="index", y="Proportion", ax=_ax1))
            _ax1.set(xlabel=col)
            _ax1.set_title(col + ", post")

        plt.close(fig)

        return fig


    def _rmse(self, targets, predictions):
        return np.sqrt(np.mean((predictions-targets)**2))


    def compare_ml_efficacy(self,
                            target_column,
                            test_data=None,
                            OHE_columns=None,
                            high_cardinality_columns=None,
                            OHE_columns_cutoff=5,
                            random_state=None,
                            train_size=0.7,
                            cv=3,
                            n_iter=5,
                            param_grid={'n_estimators': [100, 200],
                                        'max_samples': [0.6, 0.8, 1],
                                        'max_depth': [3, 4, 5]}):
        """Compares the ML efficacy of the prior data to the post data

        For a given `target_column`, this builds a ML model separately with
        `df_prior` and `df_post`, and compares the performance
        between the 2 models on a test dataset. Test data will be drawn
        from `df_post` if it is not provided.

        Args
        ----
        target_column: <str>
            Target column to be used for ML

        test_data: <pandas.DataFrame>
            Pandas dataframe of test data, to do a train test split on the
            df_post if not provided

        OHE_columns: <list of str>
            List of columns to be one hot encoded, will be determined
            if not provided

        high_cardinality_columns: <list of str>
            List of columns to be cat boost encoded, will be
            determined if not provided

        OHE_columns_cutoff: <int>
            Number of unique labels in a column to determine OHE_columns &
            high_cardinality_columns if not provided. 

        random_state: <int>
            Random state for the RandomizedSearchCV & the model fitting

        train_size: <float>
            Proportion to split the df_post by, if test_data is not provided

        cv: <int>
            Number of cross validation folds to be used in the
            RandomizedSearchCV

        n_iter: <int>
            Number of iterations for the RandomizedSearchCV

        param_grid: <dictionary of parameters>
            Dictionary of hyperparameter values to be iterated by
            the RandomizedSearchCV

        Returns
        ----
        Returns a report of ML metrics between the prior model and the
        post model
        """
        assert isinstance(target_column, str),\
            "target_column should be of type string"
        assert target_column in self.df_prior.columns,\
            "target_column does not exist in df_prior"
        assert isinstance(test_data, (pd.DataFrame, type(None))),\
            "test_data should be a pandas dataframe"
        assert isinstance(OHE_columns, (list, type(None))),\
            "OHE_columns should be of type list"
        assert isinstance(high_cardinality_columns, (list, type(None))),\
            "high_cardinality_columns should be of type list"


        # TODO: - Allow choice of model?
        #       - Allow choice of encoding for high cardinality cols?

        self.target_column = target_column
        self.train_size = train_size
        self.random_state = random_state
        self.cv = cv
        self.n_iter = n_iter
        self.param_grid = param_grid

        col_nunique = self.df_prior.nunique()

        if OHE_columns is None:
            OHE_columns = [col for col in col_nunique.index if
                        (col_nunique[col] <= OHE_columns_cutoff) &
                        (col in self.categorical_columns)]

        if high_cardinality_columns is None:
            high_cardinality_columns = [col for col in col_nunique.index if
                                     (col_nunique[col] > OHE_columns_cutoff) &
                                     (col in self.categorical_columns)]

        self.OHE_columns = OHE_columns
        self.high_cardinality_columns = high_cardinality_columns

        test_data_ = copy.deepcopy(test_data)

        if test_data_ is not None:
            test_data_[self.numeric_columns] = (
                test_data_[self.numeric_columns].astype(float)
            )
            test_data_[self.categorical_columns] = (
                test_data_[self.categorical_columns].astype(str)
            )

        self.test_data = test_data_

        self._ml_data_prep()

        if target_column in self.categorical_columns:
            self._build_classifier()
            self._eval_classifier()

        elif target_column in self.numeric_columns:
            self._build_regressor()
            self._eval_regressor()

        return self.ml_report


    def _ml_data_prep(self):
        """Prepares datasets for ML

        This does one hot encoding, cat boost encoding, and train test
        split (if necessary).
        """

        df_post = copy.deepcopy(self.df_post)
        train_prior = copy.deepcopy(self.df_prior)

        # create test data if not provided
        if self.test_data is None:

            logger.info(
                "No test data was provided. Test data will be created with",
                "a {}-{} ".format(self.train_size*100, (1-self.train_size)*100),
                "shuffle split from the post data set."
            )

            df_post = shuffle(df_post)
            n_split = int(len(df_post)*self.train_size)

            train_post = df_post.iloc[:n_split]
            test = df_post.iloc[n_split:]

        else:
            test = copy.deepcopy(self.test_data)
            train_post = df_post

        # determine columns for OHE & CatBoost
        OHE_columns = [col for col in self.OHE_columns if
                       col != self.target_column]
        high_cardinality_columns = [col for col in self.high_cardinality_columns
                                 if col != self.target_column]

        if len(OHE_columns) > 0:
            logger.info("One hot encoded columns: ", OHE_columns)
        if len(high_cardinality_columns) > 0:
            logger.info("Cat boost encoded columns: ", high_cardinality_columns)

        # concat and then OHE to ensure columns match
        train_prior['source'] = "Train Prior"
        test['source'] = "Test"
        train_post['source'] = "Train Post"

        df = pd.concat([train_prior, test, train_post])
        df = pd.get_dummies(data=df, columns=OHE_columns)

        train_prior = df[df.source == 'Train Prior'].drop('source', axis=1)
        test = df[df.source == 'Test'].drop('source', axis=1)
        train_post = df[df.source == 'Train Post'].drop('source', axis=1)

        # CatBoostEncoder for high cardinality columns
        test_prior = copy.deepcopy(test)
        test_post = copy.deepcopy(test)

        tf_prior = CatBoostEncoder(cols=high_cardinality_columns,
                                   random_state=self.random_state)
        tf_post = CatBoostEncoder(cols=high_cardinality_columns,
                                  random_state=self.random_state)

        train_prior[high_cardinality_columns] = (
            tf_prior.fit_transform(train_prior[high_cardinality_columns],
                                   train_prior[self.target_column])
        )
        test_prior[high_cardinality_columns] = (
            tf_prior.transform(test_prior[high_cardinality_columns],
                               test_prior[self.target_column])
        )
        train_post[high_cardinality_columns] = (
            tf_post.fit_transform(train_post[high_cardinality_columns],
                                  train_post[self.target_column])
        )
        test_post[high_cardinality_columns] = (
            tf_post.transform(test_post[high_cardinality_columns],
                              test_post[self.target_column])
        )

        X_train_prior = train_prior.drop(self.target_column, axis=1).astype(float)
        y_train_prior = train_prior[self.target_column].astype(float)
        X_test_prior = test_prior.drop(self.target_column, axis=1).astype(float)
        y_test = test[self.target_column].astype(float)

        X_train_post = train_post.drop(self.target_column, axis=1).astype(float)
        y_train_post = train_post[self.target_column].astype(float)
        X_test_post = test_post.drop(self.target_column, axis=1).astype(float)

        self.X_train_prior = X_train_prior
        self.y_train_prior = y_train_prior
        self.X_test_prior = X_test_prior
        self.y_test = y_test
        self.X_train_post = X_train_post
        self.y_train_post = y_train_post
        self.X_test_post = X_test_post


    def _build_regressor(self):
        """
        Builds a random forest regressor with a RandomizedSearchCV
        """

        model_prior_ = RandomForestRegressor(random_state=self.random_state)
        model_post_ = RandomForestRegressor(random_state=self.random_state)

        model_prior = RandomizedSearchCV(model_prior_,
                                         self.param_grid,
                                         n_iter=self.n_iter,
                                         cv=self.cv,
                                         random_state=self.random_state)
        model_post = RandomizedSearchCV(model_post_,
                                        self.param_grid,
                                        n_iter=self.n_iter,
                                        cv=self.cv,
                                        random_state=self.random_state)

        model_prior.fit(self.X_train_prior, self.y_train_prior)
        model_post.fit(self.X_train_post, self.y_train_post)

        logger.info(
            "A RandomForestRegressor with a RandomizedSearchCV was trained.",
            "The final model (trained with prior data) parameters are:",
            model_prior.best_estimator_,
            "The final model (trained with post data) parameters are:",
            model_post.best_estimator_
        )

        self.model_prior = model_prior
        self.model_post = model_post


    def _build_classifier(self):
        """
        Build a random forest classifier with a RandomizedSearchCV
        """

        model_prior_ = RandomForestClassifier(random_state=self.random_state)
        model_post_ = RandomForestClassifier(random_state=self.random_state)

        model_prior = RandomizedSearchCV(model_prior_,
                                        self.param_grid,
                                        n_iter=self.n_iter,
                                        cv=self.cv,
                                        random_state=self.random_state)
        model_post = RandomizedSearchCV(model_post_,
                                         self.param_grid,
                                         n_iter=self.n_iter,
                                         cv=self.cv,
                                         random_state=self.random_state)

        model_prior.fit(self.X_train_prior, self.y_train_prior)
        model_post.fit(self.X_train_post, self.y_train_post)

        logger.info(
            "A RandomForestClassifier with a RandomizedSearchCV was trained.",
            "The final model (trained with prior data) parameters are:",
            model_prior.best_estimator_,
            "The final model (trained with post data) parameters are:",
            model_post.best_estimator_
        )

        self.model_prior = model_prior
        self.model_post = model_post


    def _eval_regressor(self):
        """
        Calculates the RMSE, MAE & R2 score of the prior and post model.

        Returns a pandas dataframe of the results.
        """

        y_pred_prior = self.model_prior.predict(self.X_test_prior)
        y_pred_post = self.model_post.predict(self.X_test_post)

        rmse_prior = self._rmse(self.y_test, y_pred_prior)
        mae_prior = mean_absolute_error(self.y_test, y_pred_prior)
        r2_prior = r2_score(self.y_test, y_pred_prior)

        rmse_post = self._rmse(self.y_test, y_pred_post)
        mae_post = mean_absolute_error(self.y_test, y_pred_post)
        r2_post = r2_score(self.y_test, y_pred_post)

        res = pd.DataFrame({
            'RMSE': [rmse_prior, rmse_post],
            'MAE': [mae_prior, mae_post],
            'R2': [r2_prior, r2_post]
            },
            index=['Prior', 'Post']
        )

        self.ml_report = res


    def _eval_classifier(self):
        """
        Calculates the accuracy, precision, recall, F1 score & AUC of the
        prior and post model.

        Returns a pandas dataframe of the result.
        """

        y_pred_prior = self.model_prior.predict(self.X_test_prior)
        y_pred_post = self.model_post.predict(self.X_test_post)

        y_test_ = pd.DataFrame(self.y_test)
        y_pred_prior = pd.DataFrame(y_pred_prior, columns=y_test.columns)
        y_pred_post = pd.DataFrame(y_pred_post, columns=y_test.columns)

        y_pred_prior['source'] = "prior"
        y_pred_post['source'] = "post"
        y_test['source'] = "test"

        y_ = pd.concat([y_pred_prior, y_pred_post, y_test])
        cols = [col for col in y_.columns if col != "source"]
        y_ = pd.get_dummies(y_, columns=cols)

        y_pred_prior = y_[y_.source == 'prior'].drop('source', axis=1).values
        y_pred_post = y_[y_.source == 'post'].drop('source', axis=1).values
        y_test = y_[y_.source == 'test'].drop('source', axis=1).values

        y_.drop('source', axis=1, inplace=True)
        class_labels = y_.columns

        res = pd.DataFrame([])

        if (len(y_test[0]) == 2):
            # for binary classification
            # only take position 1, assuming position 1 is the true label
            iters = [1]
        else:
            # for multiclass classification
            iters = range(len(y_test[0]))

        for i in iters:

            precision_prior = precision_score(y_test[:,i], y_pred_prior[:,i])
            recall_prior = recall_score(y_test[:,i], y_pred_prior[:,i])
            acc_prior = accuracy_score(y_test[:,i], y_pred_prior[:,i])
            f1_prior = f1_score(y_test[:,i], y_pred_prior[:,i])
            try:
                auc_prior = roc_auc_score(y_test[:,i], y_pred_prior[:,i])
            except ValueError:
                auc_prior = "NA"

            precision_post = precision_score(y_test[:,i], y_pred_post[:,i])
            recall_post = recall_score(y_test[:,i], y_pred_post[:,i])
            acc_post = accuracy_score(y_test[:,i], y_pred_post[:,i])
            f1_post = f1_score(y_test[:,i], y_pred_post[:,i])
            try:
                auc_post = roc_auc_score(y_test[:,i], y_pred_post[:,i])
            except ValueError:
                auc_post = "NA"

            multiindex = [(str(class_labels[i]), 'Prior'),
                         (str(class_labels[i]), 'Post')]

            index = pd.MultiIndex.from_tuples(multiindex,
                                              names=['Class', 'Data Type'])

            score = pd.DataFrame({
                'Accuracy': [acc_prior, acc_post],
                'Precision': [precision_prior, precision_post],
                'Recall': [recall_prior, recall_post],
                'F1': [f1_prior, f1_post],
                'AUC': [auc_prior, auc_post]
                },
                index=index
            )

            res = pd.concat([res, score])

        self.ml_report = res
