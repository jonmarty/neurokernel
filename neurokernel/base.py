#!/usr/bin/env python

"""
Base Neurokernel classes.
"""

from contextlib import contextmanager
import copy
import multiprocessing as mp
import os
import signal
import string
import sys
import threading
import time

import bidict
import numpy as np
import scipy.sparse
import scipy as sp
import twiggy
import zmq
from zmq.eventloop.ioloop import IOLoop
from zmq.eventloop.zmqstream import ZMQStream
import msgpack_numpy as msgpack

from ctrl_proc import ControlledProcess, LINGER_TIME
from ctx_managers import IgnoreKeyboardInterrupt, OnKeyboardInterrupt, \
     ExceptionOnSignal, TryExceptionOnSignal
from tools.comm import is_poll_in
from routing_table import RoutingTable
from uid import uid

PORT_DATA = 5000
PORT_CTRL = 5001

class BaseModule(ControlledProcess):
    """
    Processing module.

    This class repeatedly executes a work method until it receives a
    quit message via its control port.

    Parameters
    ----------
    port_data : int
        Port to use when communicating with broker.
    port_ctrl : int
        Port used by broker to control module.

    Attributes
    ----------
    conn_dict dict of BaseConnectivity
       Connectivity objects connecting the module instance with
       other module instances.
    in_ids : list of int
       List of source module IDs.
    out_ids : list of int
       List of destination module IDs.

    Methods
    -------
    run()
        Body of process.
    run_step(data)
        Processes the specified data and returns a result for
        transmission to other modules.

    Notes
    -----
    If the ports specified upon instantiation are None, the module
    instance ignores the network entirely.

    Children of the BaseModule class should also contain attributes containing
    the connectivity objects.

    """

    # Define properties to perform validation when connectivity status
    # is set:
    _net = 'none'
    @property
    def net(self):
        """
        Network connectivity.
        """
        return self._net
    @net.setter
    def net(self, value):
        if value not in ['none', 'ctrl', 'in', 'out', 'full']:
            raise ValueError('invalid network connectivity value')
        self.logger.info('net status changed: %s -> %s' % (self._net, value))
        self._net = value

    def __init__(self, port_data=PORT_DATA, port_ctrl=PORT_CTRL):
                 
        super(BaseModule, self).__init__(port_ctrl, signal.SIGUSR1)

        # Logging:
        self.logger = twiggy.log.name('module %s' % self.id)

        # Data port:
        if port_data == port_ctrl:
            raise ValueError('data and control ports must differ')
        self.port_data = port_data

        # Initial connectivity:
        self.net = 'none'
        
        # Lists used for storing incoming and outgoing data; each
        # entry is a tuple whose first entry is the source or destination
        # module ID and whose second entry is the data:
        self._in_data = []
        self._out_data = []

        # Objects describing connectivity between this module and other modules
        # keyed by the IDs of the other modules:
        self._conn_dict = {}
        
    @property
    def all_ids(self):
        """
        IDs of modules to which the current module is connected.
        """

        return [c.other_mod(self.id) for c in self._conn_dict.values()]

    @property
    def in_ids(self):
        """
        IDs of modules that send data to this module.
        """

        return [c.other_mod(self.id) for c in self._conn_dict.values() if \
                c.is_connected(c.other_mod(self.id), self.id)]
    
    @property
    def out_ids(self):
        """
        IDs of modules that receive data from this module.
        """
        
        return [c.other_mod(self.id) for c in self._conn_dict.values() if \
                c.is_connected(self.id, c.other_mod(self.id))]

    def add_conn(self, conn):
        """
        Add the specified connectivity object.

        Parameters
        ----------
        conn : BaseConnectivity
            Connectivity object.

        Notes
        -----
        The module's ID must be one of the two IDs specified in the
        connnectivity object.
         
        """

        if not isinstance(conn, BaseConnectivity):
            raise ValueError('invalid connectivity object')
        if self.id not in [conn.A_id, conn.B_id]:
            raise ValueError('connectivity object must contain module ID')
        self.logger.info('connecting to %s' % conn.other_mod(self.id))

        # The connectivity instances associated with this module are keyed by
        # the ID of the other module:
        self._conn_dict[conn.other_mod(self.id)] = conn
        
        # Update internal connectivity based upon contents of connectivity
        # object. When the add_conn() method is invoked, the module's internal
        # connectivity is always upgraded to at least 'ctrl':
        if self.net == 'none':
            self.net = 'ctrl'        
        if conn.is_connected(self.id, conn.other_mod(self.id)):
            old_net = self.net
            if self.net == 'ctrl':
                self.net = 'out'
            elif self.net == 'in':
                self.net = 'full'
            self.logger.info('net status changed: %s -> %s' % (old_net, self.net))
        if conn.is_connected(conn.other_mod(self.id), self.id):
            old_net = self.net
            if self.net == 'ctrl':
                self.net = 'in'
            elif self.net == 'out':
                self.net = 'full'
            self.logger.info('net status changed: %s -> %s' % (old_net, self.net))

    def _ctrl_handler(self, msg):
        """
        Control port handler.
        """

        self.logger.info('recv ctrl message: %s' % str(msg))
        if msg[0] == 'quit':
            try:
                self.stream_ctrl.flush()
                self.stream_ctrl.stop_on_recv()
                self.ioloop_ctrl.stop()
            except IOError:
                self.logger.info('streams already closed')
            except:
                self.logger.info('other error occurred')
            self.logger.info('issuing signal %s' % self.quit_sig)
            self.sock_ctrl.send('ack')
            self.logger.info('sent to manager: ack')
            os.kill(os.getpid(), self.quit_sig)
        # One can define additional messages to be recognized by the control
        # handler:        
        # elif msg[0] == 'conn':
        #     self.logger.info('conn payload: '+str(msgpack.unpackb(msg[1])))
        #     self.sock_ctrl.send('ack')
        #     self.logger.info('sent ack') 
        else:
            self.sock_ctrl.send('ack')
            self.logger.info('sent ack')

    def _init_net(self):
        """
        Initialize network connection.
        """

        if self.net == 'none':
            self.logger.info('not initializing network connection')
        else:

            # Don't allow interrupts to prevent the handler from
            # completely executing each time it is called:
            with IgnoreKeyboardInterrupt():
                self.logger.info('initializing network connection')

                # Initialize control port handler:
                super(BaseModule, self)._init_net()

                # Use a nonblocking port for the data interface; set
                # the linger period to prevent hanging on unsent
                # messages when shutting down:
                self.sock_data = self.zmq_ctx.socket(zmq.DEALER)
                self.sock_data.setsockopt(zmq.IDENTITY, self.id)
                self.sock_data.setsockopt(zmq.LINGER, LINGER_TIME)
                self.sock_data.connect("tcp://localhost:%i" % self.port_data)
                self.logger.info('network connection initialized')

    def _get_in_data(self, in_dict):
        """
        Get input data from incoming transmission buffer.

        Input data received from other modules is used to populate the specified
        data structures.
        
        Parameters
        ----------
        in_dict : dict of numpy.ndarray of float
            Dictionary of data from other modules keyed by source module ID.
        
        """

        self.logger.info('retrieving input')        
        for entry in self._in_data:

            # Every received data packet must contain a source module ID and a payload:
            if len(entry) != 2:
                self.logger.info('ignoring invalid input data')
            else:
                in_dict[entry[0]] = entry[1]

        # Clear input buffer:
        self._in_data = []
        
    def _put_out_data(self, out):
        """
        Put output data in outgoing transmission buffer.

        Using the indices of the ports in destination modules that receive input
        from this module instance, data extracted from the module's neurons is
        staged for output transmission.

        Parameter
        ---------
        out : numpy.ndarray of float
            Output data.
        
        """

        self.logger.info('populating output buffer')

        # Clear output buffer before populating it:
        self._out_data = []

        for out_id in self.out_ids:
            out_idx = self._conn_dict[out_id].src_idx(self.id, out_id)
            self._out_data.append((out_id, np.asarray(out)[out_idx]))
        
    def _sync(self):
        """
        Send output data and receive input data.
            
        Notes
        -----
        Assumes that the attributes used for input and output already
        exist.

        Each message is a tuple containing a module ID and data; for
        outbound messages, the ID is that of the destination module.
        for inbound messages, the ID is that of the source module.
        Data is serialized before being sent and unserialized when
        received.

        """

        if self.net in ['none', 'ctrl']:
            self.logger.info('not synchronizing with network')
        else:
            self.logger.info('synchronizing with network')

            # Send outbound data:
            if self.net in ['out', 'full']:

                # Send all data in outbound buffer:
                send_ids = self.out_ids
                for out_id, data in self._out_data:
                    self.sock_data.send(msgpack.packb((out_id, data)))
                    send_ids.remove(out_id)
                    self.logger.info('sent to   %s: %s' % (out_id, str(data)))
                
                # Send data tuples containing None to those modules for which no
                # actual data was generated to satisfy the barrier condition:
                for out_id in send_ids:
                    self.sock_data.send(msgpack.packb((out_id, None)))
                    self.logger.info('sent to   %s: %s' % (out_id, None))

                # All output IDs should be sent data by this point:
                self.logger.info('sent data to all output IDs')

            # Receive inbound data:
            if self.net in ['in', 'full']:

                # Wait until inbound data is received from all source modules:  
                recv_ids = self.in_ids
                self._in_data = []
                while recv_ids:
                    in_id, data = msgpack.unpackb(self.sock_data.recv())
                    self.logger.info('recv from %s: %s ' % (in_id, str(data)))
                    recv_ids.remove(in_id)

                    # Ignore incoming data containing None:
                    if data is not None:
                        self._in_data.append((in_id, data))
                self.logger.info('recv data from all input IDs')

    def run_step(self, in_dict, out):
        """
        Perform a single step of computation.

        This method should be implemented to do something interesting with its
        arguments. It should not interact with any other class attributes.

        """

        self.logger.info('running execution step')

    def run(self):
        """
        Body of process.
        """

        with TryExceptionOnSignal(self.quit_sig, Exception, self.id):

            # Don't allow keyboard interruption of process:
            self.logger.info('starting')
            with IgnoreKeyboardInterrupt():

                self._init_net()

                in_dict = {}
                out = []
                while True:

                    # Get input data:
                    self._get_in_data(in_dict)

                    # Run the processing step:
                    self.run_step(in_dict, out)

                    # Prepare the generated data for output:
                    self._put_out_data(out)

                    # Synchronize:
                    self._sync()

            self.logger.info('exiting')

class Broker(ControlledProcess):
    """
    Broker for communicating between modules.

    Waits to receive data from all input modules before transmitting the
    collected data to destination modules.
   
    Parameters
    ----------
    port_data : int
        Port to use for communication with modules.
    port_ctrl : int
        Port used to control modules.

    Methods
    -------
    run()
        Body of process.
    sync()
        Synchronize with network.

    """

    def __init__(self, port_data=PORT_DATA, port_ctrl=PORT_CTRL,
                 routing_table=None):
        super(Broker, self).__init__(port_ctrl, signal.SIGUSR1)

        # Logging:
        self.logger = twiggy.log.name('broker %s' % self.id)

        # Data port:
        if port_data == port_ctrl:
            raise ValueError('data and control ports must differ')
        self.port_data = port_data

        # Routing table:
        self.routing_table = routing_table

        # Buffers used to accumulate data to route:
        self.data_to_route = []
        self.recv_coords_list = routing_table.coords

    def _ctrl_handler(self, msg):
        """
        Control port handler.
        """

        self.logger.info('recv: '+str(msg))
        if msg[0] == 'quit':
            try:
                self.stream_ctrl.flush()
                self.stream_data.flush()
                self.stream_ctrl.stop_on_recv()
                self.stream_data.stop_on_recv()
                self.ioloop.stop()
            except IOError:
                self.logger.info('streams already closed')
            except Exception as e:
                self.logger.info('other error occurred: '+e.message)
            self.sock_ctrl.send('ack')
            self.logger.info('sent to  broker: ack')
            # For some reason, the following lines cause problems:
            # self.logger.info('issuing signal %s' % self.quit_sig)
            # os.kill(os.getpid(), self.quit_sig)

    def _data_handler(self, msg):
        """
        Data port handler.

        Notes
        -----
        Assumes that each message contains a source module ID
        (provided by zmq) and a serialized tuple; the tuple contains
        the destination module ID and the data to be transmitted.

        """

        if len(msg) != 2:
            self.logger.info('skipping malformed message: %s' % str(msg))
        else:

            # When a message arrives, remove its source ID from the
            # list of source modules from which data is expected:
            in_id = msg[0]
            out_id, data = msgpack.unpackb(msg[1])
            self.logger.info('recv from %s: %s' % (in_id, data))
            self.logger.info('recv coords list len: '+ str(len(self.recv_coords_list)))
            if (in_id, out_id) in self.recv_coords_list:
                self.data_to_route.append((in_id, out_id, data))
                self.recv_coords_list.remove((in_id, out_id))

            # When data with source/destination IDs corresponding to
            # every entry in the routing table has been received,
            # deliver the data:
            if not self.recv_coords_list:
                self.logger.info('recv from all modules')
                for in_id, out_id, data in self.data_to_route:
                    self.logger.info('sent to   %s: %s' % (out_id, data))

                    # Route to the destination ID and send the source ID
                    # along with the data:
                    self.sock_data.send_multipart([out_id,
                                                   msgpack.packb((in_id, data))])

                # Reset the incoming data buffer and list of connection
                # coordinates:
                self.data_to_route = []
                self.recv_coords_list = self.routing_table.coords
                self.logger.info('----------------------')

    def _init_ctrl_handler(self):
        """
        Initialize control port handler.
        """

        # Set the linger period to prevent hanging on unsent messages
        # when shutting down:
        self.logger.info('initializing ctrl handler')
        self.sock_ctrl = self.zmq_ctx.socket(zmq.DEALER)
        self.sock_ctrl.setsockopt(zmq.IDENTITY, self.id)
        self.sock_ctrl.setsockopt(zmq.LINGER, LINGER_TIME)
        self.sock_ctrl.connect('tcp://localhost:%i' % self.port_ctrl)

        self.stream_ctrl = ZMQStream(self.sock_ctrl, self.ioloop)
        self.stream_ctrl.on_recv(self._ctrl_handler)

    def _init_data_handler(self):
        """
        Initialize data port handler.
        """

        # Set the linger period to prevent hanging on unsent
        # messages when shutting down:
        self.logger.info('initializing data handler')
        self.sock_data = self.zmq_ctx.socket(zmq.ROUTER)
        self.sock_data.setsockopt(zmq.LINGER, LINGER_TIME)
        self.sock_data.bind("tcp://*:%i" % self.port_data)

        self.stream_data = ZMQStream(self.sock_data, self.ioloop)
        self.stream_data.on_recv(self._data_handler)

    def _init_net(self):
        """
        Initialize the network connection.
        """

        # Since the broker must behave like a reactor, the event loop
        # is started in the main thread:
        self.zmq_ctx = zmq.Context()
        self.ioloop = IOLoop.instance()
        self._init_ctrl_handler()
        self._init_data_handler()
        self.ioloop.start()

    def run(self):
        """
        Body of process.
        """

        with TryExceptionOnSignal(self.quit_sig, Exception, self.id):
            self.recv_coords_list = self.routing_table.coords
            self._init_net()
        self.logger.info('exiting')

class BaseConnectivity(object):
    """
    Intermodule connectivity.

    Stores the connectivity between two LPUs as a series of sparse matrices.
    Every entry in an instance of the class has the following indices:

    - source module ID (must be defined upon class instantiation)
    - source port ID
    - destination module ID (must be defined upon class instantiation)
    - destination port ID
    - connection number (when two ports are connected by more than one connection)
    - parameter name (the default is 'conn' for simple connectivity)
 
    Each connection may therefore have several parameters; parameters associated
    with nonexistent connections (i.e., those whose 'conn' parameter is set to
    0) should be ignored.
    
    Parameters
    ----------
    N_A : int
        Number of ports to interface with on module A.
    N_B: int
        Number of ports to interface with on module B.
    N_mult: int
        Maximum supported number of connections between any two neurons
        (default 1). Can be raised after instantiation.
    A_id : str
        First module ID (default 'A').
    B_id : str
        Second module ID (default 'B').

    Attributes
    ----------
    nbytes : int
        Approximate number of bytes occupied by object.
    
    Methods
    -------
    N(id)
        Number of ports associated with the specified module.
    is_connected(src_id, dest_id)
        Returns True of at least one connection
        exists between `src_id` and `dest_id`.
    other_mod(id)
        Returns the ID of the other module connected by the object to
        the one specified as `id`.
    src_idx(src_id, dest_id)
        Indices of ports in module `src_id` with outgoing
        connections to module `dest_id`.
    src_mask(src_id, dest_id)
        Mask of ports in module `src_id` with outgoing
        connections to module `dest_id`.
    transpose()
        Returns a BaseConnectivity instance with the source and destination
        flipped.
    
    Examples
    --------
    The first connection between port 0 in LPU A with port 3 in LPU B can
    be accessed as c['A',0,'B',3,0]. The 'weight' parameter associated with this
    connection can be accessed as c['A',0,'B',3,0,'weight']
    
    Notes
    -----
    Since connections between LPUs should necessarily not contain any recurrent
    connections, it is more efficient to store the inter-LPU connections in two
    separate matrices that respectively map to and from the ports in each LPU
    rather than a large matrix whose dimensions comprise the total number of
    ports in both LPUs. Matrices that describe connections between A and B
    have dimensions (N_A, N_B), while matrices that describe connections between
    B and A have dimensions (N_B, N_A).
    
    """

    def __init__(self, N_A, N_B, N_mult=1, A_id='A', B_id='B'):

        # Unique object ID:
        self.id = uid()

        # The number of ports in both of the LPUs must be nonzero:
        assert N_A != 0
        assert N_B != 0

        # The maximum number of synapses between any two neurons must be
        # nonzero:
        assert N_mult != 0

        # The module IDs must be non-null and nonidentical:
        assert A_id != B_id
        assert len(A_id) != 0
        assert len(B_id) != 0
        
        self.N_A = N_A
        self.N_B = N_B
        self.N_mult = N_mult
        self.A_id = A_id
        self.B_id = B_id

        # Strings indicating direction between modules connected by instances of
        # the class:
        self._AtoB = '/'.join((A_id, B_id))
        self._BtoA = '/'.join((B_id, A_id))
        
        # All matrices are stored in this dict:
        self._data = {}

        # Keys corresponding to each connectivity direction are stored in the
        # following lists:
        self._keys_by_dir = {self._AtoB: [],
                             self._BtoA: []}

        # Create connectivity matrices for both directions; the key structure
        # is source module/dest module/connection #/parameter name. Note that
        # the matrices associated with A -> B have the dimensions (N_A, N_B)
        # while those associated with B -> have the dimensions (N_B, N_A):
        key = self._make_key(self._AtoB, 0, 'conn')
        self._data[key] = self._make_matrix((self.N_A, self.N_B), int)
        self._keys_by_dir[self._AtoB].append(key)        
        key = self._make_key(self._BtoA, 0, 'conn')
        self._data[key] = self._make_matrix((self.N_B, self.N_A), int)
        self._keys_by_dir[self._BtoA].append(key)

    def _validate_mod_names(self, A_id, B_id):
        """
        Raise an exception if the specified module names are not recognized.
        """
        
        if set((A_id, B_id)) != set((self.A_id, self.B_id)):
            raise ValueError('invalid module ID')
        
    def N(self, id):
        """
        Return number of ports associated with the specified module.
        """
        
        if id == self.A_id:
            return self.N_A
        elif id == self.B_id:
            return self.N_B
        else:
            raise ValueError('invalid module ID')

    def other_mod(self, id):
        """
        Given the specified module ID, return the ID to which the object
        connects it.
        """

        if id == self.A_id:
            return self.B_id
        elif id == self.B_id:
            return self.A_id
        else:
            raise ValueError('invalid module ID')

    def is_connected(self, src_id, dest_id):
        """
        Returns true if there is at least one connection from
        the specified source module to the specified destination module.        
        """

        self._validate_mod_names(src_id, dest_id)
        for k in self._keys_by_dir['/'.join((src_id, dest_id))]:
            if self._data[k].nnz:
                return True
        return False
    
    def src_mask(self, src_id='', dest_id='', dest_ports=slice(None, None)):
        """
        Mask of source ports with connections to destination ports.
        """

        if src_id == '' and dest_id == '':
            dir = self._AtoB
        else:
            self._validate_mod_names(src_id, dest_id)
            dir = '/'.join((src_id, dest_id))
            
        # XXX Performing a sum over the results of this list comprehension
        # might not be necessary if multapses are assumed to always have an
        # entry in the first connectivity matrix:
        m_list = [self._data[k][:,dest_ports] for k in self._keys_by_dir[dir]]
        return np.any(np.sum(m_list).toarray(), axis=1)

    def src_idx(self, src_id='', dest_id='', dest_ports=slice(None, None)):
        """
        Indices of source ports with connections to destination ports.
        """

        if src_id == '' and dest_id == '':
            src_id = self.A_id
            dest_id = self.B_id
        else:
            self._validate_mod_names(src_id, dest_id)

        mask = self.src_mask(src_id, dest_id, dest_ports)
        return np.arange(self.N(src_id))[mask]
    
    def dest_mask(self, src_id='', dest_id='', src_ports=slice(None, None)):
        """
        Mask of destination ports with connections to source ports.
        """

        if src_id == '' and dest_id == '':
            dir = self._AtoB
        else:
            self._validate_mod_names(src_id, dest_id)
            dir = '/'.join((src_id, dest_id))
            
        # XXX Performing a sum over the results of this list comprehension
        # might not be necessary if multapses are assumed to always have an
        # entry in the first connectivity matrix:
        m_list = [self._data[k][src_ports,:] for k in self._keys_by_dir[dir]]
        return np.any(np.sum(m_list).toarray(), axis=0)

    def dest_idx(self, src_id='', dest_id='', src_ports=slice(None, None)):
        """
        Indices of destination ports with connections to source ports.
        """

        if src_id == '' and dest_id == '':
            src_id = self.A_id
            dest_id = self.B_id
        else:
            self._validate_mod_names(src_id, dest_id)

        mask = self.dest_mask(src_id, dest_id, src_ports)
        return np.arange(self.N(dest_id))[mask]
    
    @property
    def nbytes(self):
        """
        Approximate number of bytes required by the class instance.

        Notes
        -----
        Only accounts for nonzero values in sparse matrices.
        """

        count = 0
        for key in self._data.keys():
            count += self._data[key].dtype.itemsize*self._data[key].nnz
        return count
    
    def _format_bin_array(self, a, indent=0):
        """
        Format a binary array for printing.
        
        Notes
        -----
        Assumes a 2D array containing binary values.
        """
        
        sp0 = ' '*indent
        sp1 = sp0+' '
        a_list = a.toarray().tolist()
        if a.shape[0] == 1:
            return sp0+str(a_list)
        else:
            return sp0+'['+str(a_list[0])+'\n'+''.join(map(lambda s: sp1+str(s)+'\n', a_list[1:-1]))+sp1+str(a_list[-1])+']'
        
    def __repr__(self):
        result = '%s -> %s\n' % (self.A_id, self.B_id)
        result += '-----------\n'
        for key in self._keys_by_dir[self._AtoB]:
            result += key + '\n'
            result += self._format_bin_array(self._data[key]) + '\n'
        result += '\n%s -> %s\n' % (self.B_id, self.A_id)
        result += '-----------\n'
        for key in self._keys_by_dir[self._BtoA]:
            result += key + '\n'
            result += self._format_bin_array(self._data[key]) + '\n'
        return result
        
    def _make_key(self, *args):
        """
        Create a unique key for a matrix of synapse properties.
        """
        
        return string.join(map(str, args), '/')

    def _make_matrix(self, shape, dtype=np.double):
        """
        Create a sparse matrix of the specified shape.
        """
        
        return sp.sparse.lil_matrix(shape, dtype=dtype)
            
    def get(self, src_id, src_idx, dest_id, dest_idx, conn=0, param='conn'):
        """
        Retrieve a value in the connectivity class instance.
        """

        if src_id == '' and dest_id == '':
            dir = self._AtoB
        else:
            self._validate_mod_names(src_id, dest_id)
        dir = '/'.join((src_id, dest_id))
        assert type(conn) == int
        
        result = self._data[self._make_key(dir, conn, param)][src_idx, dest_idx]
        if not np.isscalar(result):
            return result.toarray()
        else:
            return result

    def set(self, src_id, src_idx, dest_id, dest_idx, conn=0, param='conn', val=1):
        """
        Set a value in the connectivity class instance.

        Notes
        -----
        Creates a new storage matrix when the one specified doesn't exist.        
        """

        if src_id == '' and dest_id == '':
            dir = self._AtoB
        else:
            self._validate_mod_names(src_id, dest_id)
        dir = '/'.join((src_id, dest_id))
        assert type(conn) == int
        
        key = self._make_key(dir, conn, param)
        if not self._data.has_key(key):

            # XX should ensure that inserting a new matrix for an existing param
            # uses the same type as the existing matrices for that param XX
            if dir == self._AtoB:
                self._data[key] = \
                    self._make_matrix((self.N_A, self.N_B), type(val))
            else:
                self._data[key] = \
                    self._make_matrix((self.N_B, self.N_A), type(val))
            self._keys_by_dir[dir].append(key)

            # Increment the maximum number of connections between two ports as
            # needed:
            if conn+1 > self.N_mult:
                self.N_mult += 1
                
        self._data[key][src_idx, dest_idx] = val

    def transpose(self):
        """
        Returns an object instance with the source and destination LPUs flipped.
        """

        c = BaseConnectivity(self.N_B, self.N_A, self.N_mult,
                             A_id=self.B_id, B_id=self.A_id)
        c._keys_by_dir[self._AtoB] = []
        c._keys_by_dir[self._BtoA] = []
        for old_key in self._data.keys():

            # Reverse the direction in the key:
            key_split = old_key.split('/')
            A_id, B_id = key_split[0:2]
            new_dir = '/'.join((B_id, A_id))
            new_key = '/'.join([new_dir]+key_split[2:])
            c._data[new_key] = self._data[old_key].T           
            c._keys_by_dir[new_dir].append(new_key)
        return c

    @property
    def T(self):
        return self.transpose()
    
    def __getitem__(self, s):        
        return self.get(*s)

    def __setitem__(self, s, val):
        self.set(*s, val=val)
        
class BaseManager(object):
    """
    Module manager.

    Parameters
    ----------
    port_data : int
        Port to use for communication with modules.
    port_ctrl : int
        Port used to control modules.

    """

    def __init__(self, port_data=PORT_DATA, port_ctrl=PORT_CTRL):

        # Unique object ID:
        self.id = uid()

        self.logger = twiggy.log.name('manage %s' % self.id)
        self.port_data = port_data
        self.port_ctrl = port_ctrl

        # Set up a router socket to communicate with other topology
        # components; linger period is set to 0 to prevent hanging on
        # unsent messages when shutting down:
        self.zmq_ctx = zmq.Context()
        self.sock_ctrl = self.zmq_ctx.socket(zmq.ROUTER)
        self.sock_ctrl.setsockopt(zmq.LINGER, LINGER_TIME)
        self.sock_ctrl.bind("tcp://*:%i" % self.port_ctrl)

        # Data structures for storing broker, module, and connectivity instances:
        self.brok_dict = bidict.bidict()
        self.mod_dict = bidict.bidict()
        self.conn_dict = bidict.bidict()

        # Set up a dynamic table to contain the routing table:
        self.routing_table = RoutingTable()

    def connect(self, m_A, m_B, conn):
        """
        Connect two module instances with a connectivity object instance.

        Parameters
        ----------
        m_A, m_B : BaseModule
           Module instances to connect
        conn : BaseConnectivity
           Connectivity object instance.
                
        """

        if not isinstance(m_A, BaseModule) or \
            not isinstance(m_B, BaseModule) or \
            not isinstance(conn, BaseConnectivity):
            raise ValueError('invalid type')

        if m_A.id not in [conn.A_id, conn.B_id] or \
            m_B.id not in [conn.A_id, conn.B_id]:
            raise ValueError('connectivity object doesn\'t contain modules\' IDs')

        # Add the module and connection instances to the internal
        # dictionaries of the manager instance if they are not already there:
        if m_A.id not in self.mod_dict:
            self.add_mod(m_A)
        if m_B.id not in self.mod_dict:
            self.add_mod(m_B)
        if conn.id not in self.conn_dict:
            self.add_conn(conn)

        # Connect the modules with the specified connectivity module:
        m_A.add_conn(conn)
        m_B.add_conn(conn)

        # Update the routing table:
        if conn.is_connected(m_A.id, m_B.id):
            self.routing_table[m_A.id, m_B.id] = 1
        if conn.is_connected(m_B.id, m_A.id):
            self.routing_table[m_B.id, m_A.id] = 1

    @property
    def N_brok(self):
        """
        Number of brokers.
        """
        return len(self.brok_dict)

    @property
    def N_mod(self):
        """
        Number of modules.
        """
        return len(self.mod_dict)

    @property
    def N_conn(self):
        """
        Number of connectivity objects.
        """

        return len(self.conn_dict)

    def add_brok(self, b=None):
        """
        Add or create a broker instance to the emulation.
        """

        # TEMPORARY: only allow one broker:
        if self.N_brok == 1:
            raise RuntimeError('only one broker allowed')

        if not isinstance(b, Broker):
            b = Broker(port_data=self.port_data,
                       port_ctrl=self.port_ctrl, routing_table=self.routing_table)
        self.brok_dict[b.id] = b
        self.logger.info('added broker %s' % b.id)
        return b

    def add_mod(self, m=None):
        """
        Add or create a module instance to the emulation.
        """

        if not isinstance(m, BaseModule):
            m = BaseModule(port_data=self.port_data, port_ctrl=self.port_ctrl)
        self.mod_dict[m.id] = m
        self.logger.info('added module %s' % m.id)
        return m

    def add_conn(self, c):
        """
        Add a connectivity instance to the emulation.
        """

        if not isinstance(c, BaseConnectivity):
            raise ValueError('invalid connectivity object')
        self.conn_dict[c.id] = c
        self.logger.info('added connectivity %s' % c.id)
        return c

    def start(self):
        """
        Start execution of all processes.
        """

        with IgnoreKeyboardInterrupt():
            for b in self.brok_dict.values():
                b.start()
            for m in self.mod_dict.values():
                m.start();

    def send_ctrl_msg(self, i, *msg):
        """
        Send control message(s) to a module.
        """

        self.sock_ctrl.send_multipart([i]+msg)
        self.logger.info('sent to   %s: %s' % (i, msg))
        poller = zmq.Poller()
        poller.register(self.sock_ctrl, zmq.POLLIN)
        while True:
            if is_poll_in(self.sock_ctrl, poller):
                j, data = self.sock_ctrl.recv_multipart()
                self.logger.info('recv from %s: ack' % j)
                break

    def stop(self):
        """
        Stop execution of all processes.
        """

        self.logger.info('stopping all processes')
        poller = zmq.Poller()
        poller.register(self.sock_ctrl, zmq.POLLIN)
        recv_ids = self.mod_dict.keys()
        while recv_ids:

            # Send quit messages and wait for acknowledgments:
            i = recv_ids[0]
            self.logger.info('sent to   %s: quit' % i)
            self.sock_ctrl.send_multipart([i, 'quit'])
            if is_poll_in(self.sock_ctrl, poller):
                 j, data = self.sock_ctrl.recv_multipart()
                 self.logger.info('recv from %s: ack' % j)
                 if j in recv_ids:
                     recv_ids.remove(j)
                     self.mod_dict[j].join(1)
        self.logger.info('all modules stopped')

        # After all modules have been stopped, shut down the broker:
        for i in self.brok_dict.keys():
            self.logger.info('sent to   %s: quit' % i)
            self.sock_ctrl.send_multipart([i, 'quit'])
            self.brok_dict[i].join(1)
        self.logger.info('all brokers stopped')

def setup_logger(file_name='neurokernel.log', screen=True, port=None):
    """
    Convenience function for setting up logging with twiggy.

    Parameters
    ----------
    file_name : str
        Log file.
    screen : bool
        If true, write logging output to stdout.
    port : int
        If set to a ZeroMQ port number, publish 
        logging output to that port.

    Returns
    -------
    logger : twiggy.logger.Logger
        Logger object.

    Bug
    ---
    To use the ZeroMQ output class, it must be added as an emitter within each
    process.

    """

    if file_name:
        file_output = \
          twiggy.outputs.FileOutput(file_name, twiggy.formats.line_format, 'w')
        twiggy.addEmitters(('file', twiggy.levels.DEBUG, None, file_output))

    if screen:
        screen_output = \
          twiggy.outputs.StreamOutput(twiggy.formats.line_format,
                                      stream=sys.stdout)
        twiggy.addEmitters(('screen', twiggy.levels.DEBUG, None, screen_output))

    if port:
        port_output = ZMQOutput('tcp://*:%i' % port,
                               twiggy.formats.line_format)
        twiggy.addEmitters(('port', twiggy.levels.DEBUG, None, port_output))

    return twiggy.log.name(('{name:%s}' % 12).format(name='main'))

if __name__ == '__main__':
    from neurokernel.tools.misc import rand_bin_matrix
    
    class MyModule(BaseModule):
        """
        Example of derived module class.
        """

        def run_step(self, in_dict, out):
            super(MyModule, self).run_step(in_dict, out)
            
            out[:] = np.random.rand(3)
            
        def run(self):

            # Make each module instance generate different numbers:
            np.random.seed(int(self.id))
            super(MyModule, self).run()
            
    # Set up logging:
    logger = setup_logger()

    np.random.seed(0)

    # Set up and start emulation:
    man = BaseManager()
    man.add_brok()

    m1 = man.add_mod(MyModule())
    m2 = man.add_mod(MyModule())
    # m3 = man.add_mod(MyModule(net='full'))
    # m4 = man.add_mod(MyModule(net='full'))

    conn = BaseConnectivity(3, 3, 1, m1.id, m2.id)
    conn[m1.id, :, m2.id, :] = [[1, 0, 0],
                                [0, 1, 0],
                                [0, 0, 1]]    
    conn[m2.id, :, m1.id, :] = [[1, 0, 0],
                                [0, 1, 0],
                                [0, 0, 1]]    
    man.add_conn(conn)
    man.connect(m1, m2, conn)
    
    # man.connect(m2, m1, conn)
    # man.connect(m4, m3, conn)
    # man.connect(m3, m4, conn)
    # man.connect(m4, m1, conn)
    # man.connect(m1, m4, conn)
    # man.connect(m2, m4, conn)
    # man.connect(m4, m2, conn)

    man.start()
    time.sleep(1)
    man.stop()
    logger.info('all done')