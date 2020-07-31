### Be noted: this script is developed against the model exported from Megatron GPT2 Pretraining script.

import sys
import onnx
from onnx import helper, shape_inference
from onnx import TensorProto
import numpy as np
from onnx import numpy_helper

def add_name(model):
    i = 0
    for node in model.graph.node:
       node.name = '%s_%d' %(node.op_type, i)
       i += 1

def find_input_node(model, arg):
    result = []
    for node in model.graph.node:
        for output in node.output:
            if output == arg:
                result.append(node)
    return result[0] if len(result)== 1 else None

def find_output_node(model, arg):
    result = []
    for node in model.graph.node:
        for input in node.input:
            if input == arg:
                result.append(node)
    return result[0] if len(result) == 1 else None

def find_initializer(model, arg):
    for initializer in model.graph.initializer:
        if initializer.name == arg:
            return initializer
    return None

def find_input(model, arg):
    for graph_input in model.graph.input:
        if graph_input.name == arg:
            return graph_input
    return None

def find_all_fused_nodes(model, concat_node):
    result = []
    candidate = [concat_node]
    while len(candidate) > 0:
        node = candidate[0]
        candidate.pop(0)
        result.append(node)
        if node.op_type == 'Shape':
            continue
        for input in node.input:
            input_node = find_input_node(model, input)
            if input_node is not None:
                candidate.append(input_node)
    return result

def get_node_index(model, node):
    i = 0
    while i < len(model.graph.node):
        if model.graph.node[i] == node:
            break
        i += 1
    return i if i < len(model.graph.node) else None

def add_const(model, name, output, t_value = None, f_value = None):
    const_node = model.graph.node.add()
    const_node.op_type = 'Constant'
    const_node.name = name
    const_node.output.extend([output])
    attr = const_node.attribute.add()
    attr.name = 'value'
    if t_value is not None:
        attr.type = 4
        attr.t.CopyFrom(t_value)
    else:
        attr.type = 1
        attr.f = f_value
    return const_node

def process_concat(model):
    new_nodes = {}
    delete_nodes = []
    for node in model.graph.node:
        if node.op_type != 'Concat':
            continue
        skip = False
        input_nodes = []
        for input in node.input:
            concat_input_node = find_input_node(model, input)
            if concat_input_node.op_type != 'Unsqueeze':
                skip = True
            input_nodes.append(concat_input_node)

        if skip == True:
            continue

        #figure out target shape
        shape = []
        special=True
        for input_node in input_nodes:
           
            const_input = find_input_node(model, input_node.input[0])
            if const_input.op_type != 'Constant':
                shape.append(0)
            else:
                special=False
                attr = const_input.attribute
                assert len(attr) == 1
                assert attr[0].name == 'value'
                assert attr[0].type == 4
                data = numpy_helper.to_array(attr[0].t)
                shape.append(np.asscalar(data))

        reshape_node = find_output_node(model, node.output[0])
        if reshape_node:
            assert reshape_node.op_type == 'Reshape'
            new_nodes[get_node_index(model, reshape_node)] = shape
            #find out the nodes need to be deleted.
            fuse_nodes = find_all_fused_nodes(model, node)
            for n in fuse_nodes:
                delete_nodes.append(get_node_index(model, n))
        else:
            continue

    #insert new shape to reshape
    index = 0
    for reshape_node_index in new_nodes:
        shape_tensor = numpy_helper.from_array(np.asarray(new_nodes[reshape_node_index], dtype=np.int64))
        const_node = add_const(model, 'concat_shape_node_%d' % index, 'concat_shape_%d' % index, shape_tensor)
        index+=1
        reshape_node = model.graph.node[reshape_node_index]
        reshape_node.input[1] = const_node.output[0]
    #delete nodes
    delete_nodes.sort(reverse=True)
    for delete_node in delete_nodes:
        del model.graph.node[delete_node]

def replace_input_arg(model, arg, new_arg):
    for node in model.graph.node:
        i = 0
        while i < len(node.input):
            if node.input[i] == arg:
                node.input[i] = new_arg
            i += 1

def find_weight_index(model, name):
    index = 0
    for w in model.graph.initializer:
        if w.name == name:
            return index
        index += 1
    return None

def find_input_index(model, name):
    index = 0
    for w in model.graph.input:
        if w.name == name:
            return index
        index += 1
    return None

def fix_transpose(model):
    transpose = []
    for node in model.graph.node:
        if node.op_type == 'Transpose':
            weight = find_initializer(model, node.input[0])
            if weight is not None:
                result = []
                for n in model.graph.node:
                    for input in n.input:
                        if input == weight.name:
                            result.append(n)
                if len(result) > 1:
                    continue
                perm = node.attribute[0]
                assert perm.name == 'perm'
                perm = perm.ints
                assert len(perm) == 2 and perm[0] == 1 and perm[1] == 0
                transpose.append((get_node_index(model, node), weight))
    for t in transpose:
        node = model.graph.node[t[0]]
        weight = numpy_helper.to_array(t[1])
        assert len(weight.shape) == 2
        weight = weight.transpose(perm)
        new_weight = numpy_helper.from_array(weight, "%s_transposed" % t[1].name)
        model.graph.initializer.extend([new_weight])
        replace_input_arg(model, node.output[0], new_weight.name)

    transpose.sort(reverse=True)
    for t in transpose:
        del model.graph.node[t[0]]

    old_ws = []
    old_graph_inputs=[]
    for t in transpose:
        if find_output_node(model, t[1].name) is None:
            old_ws.append(find_weight_index(model, t[1].name))
            old_graph_inputs.append(find_input_index(model, t[1].name))
    old_ws.sort(reverse=True)
    old_graph_inputs.sort(reverse=True)

    for g_i in old_graph_inputs:
        del model.graph.input[g_i]

    #clean up old weights
    for w_i in old_ws:
        del model.graph.initializer[w_i]

def process_dropout(model):
    dropouts = []
    index = 0
    for node in model.graph.node:
        if node.op_type == 'Dropout':
            new_dropout = model.graph.node.add()
            new_dropout.op_type = 'TrainableDropout'
            new_dropout.name = 'TrainableDropout_%d' % index

            # make ratio node
            ratio = np.asarray([node.attribute[0].f], dtype=np.float32)
            ratio_value = numpy_helper.from_array(ratio)
            ratio_node = add_const(model, 'dropout_node_ratio_%d' % index, 'dropout_node_ratio_%d' % index, t_value=ratio_value)

            new_dropout.input.extend([node.input[0], ratio_node.output[0]])
            new_dropout.output.extend(node.output)
            dropouts.append(get_node_index(model, node))
            index += 1
    dropouts.sort(reverse=True)
    for d in dropouts:
        del model.graph.node[d]

def get_nodes_to_remove(input_id):
    cast_node3 = find_input_node(model, input_id)
    not_node3 = find_input_node(model, cast_node3.input[0])
    if not_node3.op_type == "Not":
        less_node = find_input_node(model, not_node3.input[0])
    else:
        assert not_node3.op_type == "Less"
        less_node = not_node3
    for less_input in less_node.input:
        less_input_node = find_input_node(model, less_input)
        if less_input_node and less_input_node.op_type == "Constant":
            const_node = less_input_node
            break
    return [cast_node3, not_node3, less_node, const_node]

def transform_gpt2(model):
    #add name to nodes
    add_name(model)

    #replace shape-gather-unsqueeze-concat of reshape with const shape
    process_concat(model)
    
    #constant fold transpose
    fix_transpose(model)

    #replace dropout with trainable dropout
    process_dropout(model)

    #set opset version to 11
    model.opset_import[0].version = 11

