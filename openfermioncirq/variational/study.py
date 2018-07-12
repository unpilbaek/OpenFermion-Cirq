#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

from typing import (TYPE_CHECKING, Any, Dict, Hashable, Iterable, Optional,
                    Sequence, Tuple, Union)

import collections
import multiprocessing
import os
import pickle

import numpy

import cirq
from cirq import abc

from openfermioncirq.variational import VariationalAnsatz
from openfermioncirq.optimization import (
        BlackBox,
        OptimizationAlgorithm,
        OptimizationResult,
        OptimizationTrialResult)

if TYPE_CHECKING:
    # pylint: disable=unused-import
    from typing import List


class OptimizationParams:
    """Parameters for an optimization run of a variational study.

    Attributes:
        algorithm: The algorithm to use.
        initial_guess: An initial guess for the algorithm to use. If not
            specified, then it will be set to the default initial parameters
            of the study.
        initial_guess_array: An array of initial guesses for the algorithm
            to use. If not specified, then it will be set to an array
            containing the default initial parameters of the study.
        cost_of_evaluate: An optional cost to be used by the `evaluate`
            method of the BlackBox that will be optimized.
        reevaluate_final_params: Whether the optimal parameters returned
            by the optimization algorithm should be reevaluated using the
            `evaluate` method of the study and the optimal value adjusted
            accordingly. This is useful when the optimizer only has access
            to the noisy `evaluate_with_cost` method of the study (because
            `cost_of_evaluate` is set), but you are interested in the true
            noiseless value of the returned parameters.
    """

    def __init__(self,
                 algorithm: OptimizationAlgorithm,
                 initial_guess: Optional[numpy.ndarray]=None,
                 initial_guess_array: Optional[numpy.ndarray]=None,
                 cost_of_evaluate: Optional[float]=None,
                 reevaluate_final_params: bool=False) -> None:
        """Construct a parameters object by setting its attributes."""
        self.algorithm = algorithm
        self.initial_guess = initial_guess
        self.initial_guess_array = initial_guess_array
        self.cost_of_evaluate = cost_of_evaluate
        self.reevaluate_final_params = reevaluate_final_params


class VariationalStudy(metaclass=abc.ABCMeta):
    """The results from optimizing a variational ansatz.

    A variational study has a way of assigning a numerical value, or score, to
    the output of its ansatz circuit. The goal of a variational quantum
    algorithm is to find a setting of parameters that minimizes the value of the
    resulting circuit output.

    The VariationalStudy class supports the option to provide a noise and cost
    model for evaluations. This is useful for modeling situations in which the
    value of the ansatz can be determined only approximately and there is a
    tradeoff between the accuracy of the evaluation and the cost of the
    evaluation. As an example, suppose the value of the ansatz is equal to the
    expectation value of a Hamiltonian on the final state output by the circuit,
    and one estimates this value by measuring the Hamiltonian. The average value
    of the measurements over multiple runs gives an estimate of the expectation
    value, but it will not, in general, be the exact value. Rather, it can
    usually be accurately modeled as a random value drawn from a normal
    distribution centered at the true value and whose variance is inversely
    proportional to the number of measurements taken. This can be simulated by
    computing the true expectation value and adding artificial noise drawn from
    a normal distribution with the desired variance.

    Example:
        ansatz = SomeVariationalAnsatz()
        study = SomeVariationalStudy('my_study', ansatz)
        optimize_params = OptimizationParams(
            algorithm=openfermioncirq.optimization.COBYLA,
            initial_guess=numpy.zeros(5))
        study.optimize('run1', optimize_params)  # the result is saved into
                                                 # study.results
        result, params = study.results['run1']
        print(result.optimal_value)  # prints a number
        print(params.initial_guess)  # prints the initial guess used

    Attributes:
        name: The name of the study.
        circuit: The circuit of the study, which is the preparation circuit, if
            any, followed by the ansatz circuit.
        qubits: A list containing the qubits used by the circuit.
        results: A dictionary of tuples. The first element of each tuple is an
            OptimizationTrialResult containing the results of an optimization
            run, and the second element of the tuple is the
            OptimizationParams object giving the parameters of the
            optimization run that produced that result. Key is an arbitrary
            identifier used to label the run.
        num_params: The number of parameters in the circuit.
    """

    def __init__(self,
                 name: str,
                 ansatz: VariationalAnsatz,
                 preparation_circuit: Optional[cirq.Circuit]=None,
                 datadir: Optional[str]=None) -> None:
        """
        Args:
            name: The name of the study.
            ansatz: The ansatz to study.
            preparation_circuit: A circuit to apply prior to the ansatz circuit.
                It should use the qubits belonging to the ansatz.
            datadir: The directory to use when saving the study. The default
                behavior is to use the current working directory.
        """
        # TODO store results as a pandas DataFrame?
        self.name = name
        self.results = collections.OrderedDict() \
          # type: Dict[Any, Tuple[OptimizationTrialResult, OptimizationParams]]
        self._ansatz = ansatz
        self._preparation_circuit = preparation_circuit or cirq.Circuit()
        self._circuit = self._preparation_circuit + self._ansatz.circuit
        self._num_params = len(self.param_names())
        self.datadir = datadir

    @abc.abstractmethod
    def value(self,
              trial_result: Union[cirq.TrialResult,
                                  cirq.google.XmonSimulateTrialResult]
              ) -> float:
        """The evaluation function for a circuit output.

        A variational quantum algorithm will attempt to minimize this value over
        possible settings of the parameters.
        """
        pass

    def noise(self, cost: Optional[float]=None) -> float:
        """Artificial noise that may be added to the true ansatz value.

        The `cost` argument is used to model situations in which it is possible
        to reduce the magnitude of the noise at some cost.
        """
        # Default: no noise
        return 0.0

    def evaluate(self,
                 param_values: numpy.ndarray) -> float:
        """Determine the value of some parameters."""
        # Default: evaluate using Xmon simulator
        simulator = cirq.google.XmonSimulator()
        result = simulator.simulate(
                     self.circuit,
                     param_resolver=self._ansatz.param_resolver(param_values))
        return self.value(result)

    def evaluate_with_cost(self,
                           param_values: numpy.ndarray,
                           cost: float) -> float:
        """Evaluate parameters with a specified cost."""
        # Default: add artifical noise with the specified cost
        return self.evaluate(param_values) + self.noise(cost)

    def noise_bounds(self,
                     cost: float,
                     confidence: Optional[float]=None
                     ) -> Tuple[float, float]:
        """Exact or approximate bounds on noise in the objective function.

        Returns a tuple (a, b) such that when `evaluate_with_cost` is called
        with the given cost and returns an approximate function value y, the
        true function value lies in the interval [y + a, y + b]. Thus, it should
        be the case that a <= 0 <= b.

        This function takes an optional `confidence` parameter which is a real
        number strictly between 0 and 1 that gives the probability of the bounds
        being correct. This is used for situations in which exact bounds on the
        noise cannot be guaranteed.
        """
        return -numpy.inf, numpy.inf

    def optimize(self,
                 identifier: Hashable,
                 optimize_params: OptimizationParams,
                 repetitions: int=1,
                 seeds: Optional[Sequence[int]]=None,
                 use_multiprocessing: bool=False,
                 num_processes: Optional[int]=None) -> None:
        """Perform an optimization run and save the results.

        Constructs a BlackBox that uses the study to perform function
        evaluations, then uses the given algorithm to optimize the BlackBox.
        The result is saved into a list in the `results` dictionary of the study
        under the key specified by `identifier`.

        The `cost_of_evaluate` argument affects how the BlackBox is constructed.
        If it is None, then the `evaluate` method of the BlackBox will call the
        `evaluate` method of the study. If it is not None, then the `evaluate`
        method of the BlackBox will call the `evaluate_with_cost` method of the
        study using this cost as input.

        Args:
            identifier: An identifier for the run. This is used as the key to
                `self.results`, where results are saved.
            optimize_params: The parameters of the optimization run.
            repetitions: The number of times to run the optimization.
            seeds: Random number generator seeds to use for the repetitions.
                The default behavior is to randomly generate an independent seed
                for each repetition.
            use_multiprocessing: Whether to use multiprocessing to run
                repetitions in different processes.
            num_processes: The number of processes to use for multiprocessing.
                The default behavior is to use the output of
                `multiprocessing.cpu_count()`.
        """
        self.optimize_sweep([identifier],
                            [optimize_params],
                            repetitions,
                            seeds,
                            use_multiprocessing,
                            num_processes)

    def optimize_sweep(self,
                       identifiers: Iterable[Hashable],
                       params: Iterable[OptimizationParams],
                       repetitions: int=1,
                       seeds: Optional[Sequence[int]]=None,
                       use_multiprocessing: bool=False,
                       num_processes: Optional[int]=None) -> None:
        """Perform multiple optimization runs and save the results.

        This is like `optimize`, but lets you specify multiple
        OptimizationParams to use for separate runs.

        Args:
            identifiers: Identifiers for the runs, one for each
                OptimizationParams object provided. This is used as the key
                to `self.results`, where results are saved.
            params: The parameters for the optimization runs.
            repetitions: The number of times to run the algorithm for each
                inititial guess.
            seeds: Random number generator seeds to use for the repetitions.
                The default behavior is to randomly generate an independent seed
                for each repetition.
            use_multiprocessing: Whether to use multiprocessing to run
                repetitions in different processes.
            num_processes: The number of processes to use for multiprocessing.
                The default behavior is to use the output of
                `multiprocessing.cpu_count()`.
        """
        if seeds is not None and len(seeds) < repetitions:
            raise ValueError(
                    "Provided fewer RNG seeds than the number of repetitions.")

        for identifier, optimize_params in zip(identifiers, params):
            if use_multiprocessing:
                if num_processes is None:
                    num_processes = multiprocessing.cpu_count()
                with multiprocessing.Pool(num_processes) as pool:
                    result_list = pool.starmap(
                            self._run_optimization,
                            ((optimize_params,
                              seeds[i] if seeds is not None
                                  else numpy.random.randint(4294967296))
                              for i in range(repetitions)))
            else:
                result_list = []
                for i in range(repetitions):
                    result = self._run_optimization(
                            optimize_params,
                            seeds[i] if seeds is not None
                                else numpy.random.randint(4294967296))
                    result_list.append(result)
            self.results[identifier] = (OptimizationTrialResult(result_list),
                                        optimize_params)

    def _run_optimization(
            self,
            optimize_params: OptimizationParams,
            seed: int) -> OptimizationResult:
        """Perform an optimization run and return the result.

        If no initial guess is given, the default initial parameters of the
        study are used.
        """

        black_box = VariationalStudyBlackBox(
                self,
                cost_of_evaluate=optimize_params.cost_of_evaluate)
        initial_guess = optimize_params.initial_guess
        initial_guess_array = optimize_params.initial_guess_array

        if initial_guess is None:
            initial_guess = self.default_initial_params()
        if initial_guess_array is None:
            initial_guess_array = numpy.array([self.default_initial_params()])

        numpy.random.seed(seed)
        result = optimize_params.algorithm.optimize(black_box,
                                                        initial_guess,
                                                        initial_guess_array)

        result.num_evaluations = black_box.num_evaluations
        result.cost_spent = black_box.cost_spent
        result.seed = seed
        if optimize_params.reevaluate_final_params:
            result.optimal_value = self.evaluate(result.optimal_parameters)

        return result

    @property
    def summary(self) -> str:
        header = []   # type: List[str]
        details = []  # type: List[str]
        optimal_value = numpy.inf
        optimal_identifier = None  # type: Optional[Hashable]

        for identifier, (result, _) in self.results.items():
            result_opt = result.optimal_value
            if result_opt < optimal_value:
                optimal_value = result_opt
                optimal_identifier = identifier
            details.append(
                    '    Identifier: {}'.format(identifier))
            details.append(
                    '        Optimal value: {}'.format(result_opt))
            details.append(
                    '        Number of repetitions: {}'.format(
                        result.repetitions))
            details.append(
                    '        Optimal value 1st, 2nd, 3rd quartiles:')
            details.append(
                    '            {}'.format(
                        list(result.optimal_value_quantile([.25, .5, .75]))))
            details.append(
                    '        Num evaluations 1st, 2nd, 3rd quartiles:')
            details.append(
                    '            {}'.format(
                        list(result.num_evaluations_quantile([.25, .5, .75]))))
            details.append(
                    '        Cost spent 1st, 2nd, 3rd quartiles:')
            details.append(
                    '            {}'.format(
                        list(result.cost_spent_quantile([.25, .5, .75]))))

        header.append(
                'This study contains {} results.'.format(len(self.results)))
        header.append(
                'The optimal value found among all results is {}.'.format(
                    optimal_value))
        header.append(
                'It was found by the run with identifier {}.'.format(
                    repr(optimal_identifier)))
        header.append('Result details:')

        return '\n'.join(header + details)

    @property
    def circuit(self) -> cirq.Circuit:
        """The preparation circuit followed by the ansatz circuit."""
        return self._circuit

    @property
    def qubits(self) -> cirq.Circuit:
        """The qubits used by the study circuit."""
        return self._ansatz.qubits

    @property
    def num_params(self) -> int:
        """The number of parameters of the ansatz."""
        return self._num_params

    # TODO expose ansatz instead of the methods below
    def param_names(self) -> Sequence[str]:
        """The names of the parameters of the ansatz."""
        return self._ansatz.param_names()

    def param_bounds(self) -> Optional[Sequence[Tuple[float, float]]]:
        """Optional bounds on the parameters."""
        return self._ansatz.param_bounds()

    def default_initial_params(self) -> numpy.ndarray:
        """Suggested initial parameter settings."""
        return self._ansatz.default_initial_params()

    def _init_kwargs(self) -> Dict[str, Any]:
        """Arguments to pass to __init__ when re-loading the study.

        Subclasses that override __init__ may need to override this method for
        saving and loading to work properly.
        """
        return {'name': self.name,
                'ansatz': self._ansatz,
                'preparation_circuit': self._preparation_circuit}

    def save(self) -> None:
        """Save the study to disk."""
        filename = '{}.study'.format(self.name)
        if self.datadir is not None:
            filename = os.path.join(self.datadir, filename)
            if not os.path.isdir(self.datadir):
                os.mkdir(self.datadir)
        with open(filename, 'wb') as f:
            pickle.dump((type(self), self._init_kwargs(), self.results), f)

    @staticmethod
    def load(name: str, datadir: Optional[str]=None) -> 'VariationalStudy':
        """Load a study from disk.

        Args:
            name: The name of the study.
            datadir: The directory where the study file is saved.
        """
        if name.endswith('.study'):
            filename = name
        else:
            filename = '{}.study'.format(name)
        if datadir is not None:
            filename = os.path.join(datadir, filename)
        with open(filename, 'rb') as f:
            cls, kwargs, results = pickle.load(f)
        study = cls(datadir=datadir, **kwargs)
        for key, val in results.items():
            study.results[key] = val
        return study


class VariationalStudyBlackBox(BlackBox):
    """A black box for evaluations in a variational study.

    This black box keeps track of the number of times it has been evaluated as
    well as the total cost that has been spent on evaluations according to the
    noise and cost model of the study.

    Attributes:
        study: The variational study whose evaluation functions are being
            encapsulated by the black box
        num_evaluations: The number of times the objective function has been
            evaluated, including noisy evaluations.
        cost_spent: The total cost that has been spent on function evaluations.
        cost_of_evaluate: An optional cost to be used by the `evaluate` method.
    """
    # TODO implement cost budget
    # TODO save the points that were evaluated

    def __init__(self,
                 study: VariationalStudy,
                 cost_of_evaluate: Optional[float]=None) -> None:
        self.study = study
        self.num_evaluations = 0
        self.cost_spent = 0.0
        self.cost_of_evaluate = cost_of_evaluate

    @property
    def dimension(self) -> int:
        """The dimension of the array accepted by the objective function."""
        return self.study.num_params

    @property
    def bounds(self) -> Optional[Sequence[Tuple[float, float]]]:
        """Optional bounds on the inputs to the objective function."""
        return self.study.param_bounds()

    def evaluate(self,
                 x: numpy.ndarray) -> float:
        """Evaluate the objective function.

        If `cost_of_evaluate` is None, then this just calls `study.evaluate`.
        Otherwise, it calls `study.evaluate_with_cost` with with that cost.

        Side effects: Increments self.num_evaluations by one.
            If cost_of_evaluate is not None, then its value is added to
            self.cost_spent.
        """
        self.num_evaluations += 1
        if self.cost_of_evaluate is None:
            return self.study.evaluate(x)
        else:
            self.cost_spent += self.cost_of_evaluate
            return self.study.evaluate_with_cost(x, self.cost_of_evaluate)

    def evaluate_with_cost(self,
                           x: numpy.ndarray,
                           cost: float) -> float:
        """Evaluate the objective function with a specified cost.

        Side effects: Increments self.num_evaluations by one and adds the cost
            spent to self.cost_spent.
        """
        self.num_evaluations += 1
        self.cost_spent += cost
        return self.study.evaluate_with_cost(x, cost)

    def noise_bounds(self,
                     cost: float,
                     confidence: Optional[float]=None
                     ) -> Tuple[float, float]:
        """Exact or approximate bounds on noise in the objective function."""
        return self.study.noise_bounds(cost, confidence)
