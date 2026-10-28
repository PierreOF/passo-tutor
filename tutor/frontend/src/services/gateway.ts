import { HttpTutorGateway } from './httpTutorGateway'
import { MockTutorGateway } from './mockTutorGateway'

const useMock = import.meta.env.MODE === 'test' || import.meta.env.VITE_TUTOR_GATEWAY === 'mock'

export const tutorGateway = useMock ? new MockTutorGateway() : new HttpTutorGateway()
